"""

Code adjusted from https://github.com/kamalkraj/BERT-NER

Developed for the SIGIR 2020 paper: Query Resolution for Conversational Search with Limited Supervision, by Voskarides et al.

"""

from __future__ import absolute_import, division, print_function, unicode_literals
import argparse
import glob
import json
import logging
import os
import random
import time

# from torch.utils.tensorboard import SummaryWriter
from tensorboardX import SummaryWriter

import numpy as np
import torch
import torch.nn.functional as F
from pytorch_transformers import (WEIGHTS_NAME, AdamW, BertConfig,
                                  BertForTokenClassification, BertTokenizer,
                                  WarmupLinearSchedule)
from torch import nn
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from tools import eval_seq_labeling as eval_seq_labeling_token

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)


class Ner(BertForTokenClassification):

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None,valid_ids=None,
                attention_mask_label=None):
        sequence_output = self.bert(input_ids, token_type_ids, attention_mask,head_mask=None)[0]
        batch_size,max_len,feat_dim = sequence_output.shape
        valid_output = torch.zeros(batch_size,max_len,feat_dim,dtype=torch.float32,device='cuda')
        #valid_output.to(device="cuda:1")

        for i in range(batch_size):
            jj = -1
            for j in range(max_len):
                    if valid_ids[i][j].item() == 1:
                        jj += 1
                        valid_output[i][jj] = sequence_output[i][j]
        sequence_output = self.dropout(valid_output)
        #sequence_output = sequence_output.to(device="cuda:1")
        #logger.info("sequence_output"+str(sequence_output.device))
        logits = self.classifier(sequence_output)

        if labels is not None:
            loss_fct = nn.CrossEntropyLoss(ignore_index=0)
            # Only keep active parts of the loss
            #attention_mask_label = None
            if attention_mask_label is not None:
                active_loss = attention_mask_label.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            return loss
        else:
            return logits


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id, valid_ids=None, label_mask=None, _id=None):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id
        self.valid_ids = valid_ids
        self.label_mask = label_mask
        self._id = _id


def readfile(filename):
    '''
    read file
    '''
    f = open(filename)
    data = []
    sentence = []
    label= []
    for line in f:
        if len(line)==0 or line.startswith('-DOCSTART') or line[0]=="\n":
            if len(sentence) > 0:
                data.append((sentence,label))
                sentence = []
                label = []
            continue
        splits = line.split(' ')
        sentence.append(splits[0])
        label.append(splits[-1][:-1])

    if len(sentence) > 0:
        data.append((sentence,label))
        sentence = []
        label = []
    return data


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        return readfile(input_file)


class NerProcessor(DataProcessor):
    """Processor for the CoNLL-2003 data set."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train.txt")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "valid.txt")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "test.txt")), "test")

    def get_labels(self):
        return ["O", "B-MISC", "I-MISC",  "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC", "[CLS]", "[SEP]"]

    def _create_examples(self,lines,set_type):
        examples = []
        for i,(sentence, label) in enumerate(lines):
            guid = "%s-%s" % (set_type, i)
            text_a = ' '.join(sentence)
            text_b = None
            label = label
            examples.append(InputExample(guid=guid,text_a=text_a,text_b=text_b,label=label))
        return examples


class ConvSearchProcessor(NerProcessor):
    """Processor for Conversational Search datasets.
    """

    def __init__(self, add_sep=True, part='bert_ner_overlap', train_on='train_quac', dev_on='train_cast'):
        # add_sep: whether to add a separator token before the last query
        logger.debug('add_sep', add_sep)
        # self.add_sep = add_sep
        self.part = part
        self.train_on = train_on
        self.dev_on = dev_on

    def read_json_file(self, path, uppercase=False):
        data = [self._get_line(line, uppercase)
                for line in json.load(open(path))]
        return data

    def _get_line(self, line, uppercase):
        _id = line['id']
        tokens, labels = line[self.part]
        return _id, tokens, labels

    def get_train_examples(self, data_dir, portion=1.0, uppercase=False):
        """See base class.
        Portion is used for sampling a part of the training dataset (0, 1)
        """

        assert 0 < portion <= 1.0

        file_content = self.read_json_file(os.path.join(data_dir, "{}.json".format(self.train_on)),
                                           uppercase=uppercase)
        if portion < 1.0:
            # sample by conversation id

            num_qids = len(file_content)

            separator = '#' if 'quac' in self.train_on else '_'
            topic_ids = set()
            qid2topicid = dict()
            for qid, _, _ in file_content:
                topic_id = qid[:qid.index(separator)]
                topic_ids.add(topic_id)
                qid2topicid[qid] = topic_id

            num_topics_to_sample = int(portion * len(topic_ids))
            sampled_topic_ids = random.sample(topic_ids, num_topics_to_sample)

            logger.info('Sampled {} / {} conversations'.format(len(sampled_topic_ids), len(topic_ids)))

            file_content = [(qid, tokens, labels) for qid, tokens, labels in file_content
                            if qid2topicid[qid] in sampled_topic_ids]

            logger.info('Sampled {} / {} qids'.format(len(file_content), num_qids))

        return self._create_examples(file_content, "train")

    def get_dev_examples(self, data_dir, uppercase=False):
        """See base class."""
        return self._create_examples(
            self.read_json_file(os.path.join(data_dir, "{}.json".format(self.dev_on)),
                                uppercase=uppercase), "dev")

    def get_test_examples(self, data_dir, uppercase=False):
        """See base class."""
        return self._create_examples(
            self.read_json_file(os.path.join(data_dir, "test.json"),
                                uppercase=uppercase), "test")

    def get_labels(self):
        return ["O", "REL", "[CLS]", "[SEP]"]

    def _create_examples(self,lines,set_type):
        examples = []
        for i,(_id, sentence, label) in enumerate(lines):
            text_a = ' '.join(sentence)
            text_b = None
            label = label
            examples.append(InputExample(guid=_id,text_a=text_a,text_b=text_b,label=label))
        return examples


def convert_examples_to_features(examples, label_list, max_seq_length, tokenizer):
    """Loads a data file into a list of `InputBatch`s."""

    label_map = {label : i for i, label in enumerate(label_list,1)}

    logger.info('Converting examples to features...')
    s_time = time.time()

    features = []
    for (ex_index,example) in enumerate(examples):
        textlist = example.text_a.split(' ')
        labellist = example.label
        tokens = []
        labels = []
        valid = []
        label_mask = []
        if len(textlist) != len(labellist):
            print(ex_index)
            print(textlist, labellist)
            print(len(textlist), len(labellist))
        for i, word in enumerate(textlist):
            token = tokenizer.tokenize(word)
            tokens.extend(token)
            label_1 = labellist[i]
            for m in range(len(token)):
                if m == 0:
                    labels.append(label_1)
                    valid.append(1)
                    label_mask.append(1)
                else:
                    valid.append(0)
        if len(tokens) >= max_seq_length - 1:
            tokens = tokens[0:(max_seq_length - 2)]
            labels = labels[0:(max_seq_length - 2)]
            valid = valid[0:(max_seq_length - 2)]
            label_mask = label_mask[0:(max_seq_length - 2)]
        ntokens = []
        segment_ids = []
        label_ids = []
        ntokens.append("[CLS]")
        segment_ids.append(0)
        valid.insert(0,1)
        label_mask.insert(0,1)
        label_ids.append(label_map["[CLS]"])
        for i, token in enumerate(tokens):
            ntokens.append(token)
            segment_ids.append(0)
            if len(labels) > i:
                label_ids.append(label_map[labels[i]])
        ntokens.append("[SEP]")
        segment_ids.append(0)
        valid.append(1)
        label_mask.append(1)
        label_ids.append(label_map["[SEP]"])
        input_ids = tokenizer.convert_tokens_to_ids(ntokens)

        input_mask = [1] * len(input_ids)

        # mask out labels for current turn.
        cur_turn_index = label_ids.index(label_map['[SEP]'])

        label_mask = [1] * cur_turn_index + [0] * (len(label_ids) - cur_turn_index)

        assert len(label_ids) == len(label_mask)

        while len(input_ids) < max_seq_length:
            input_ids.append(tokenizer.pad_token_id)
            input_mask.append(0)
            segment_ids.append(0)
            label_ids.append(0)
            valid.append(1)
            label_mask.append(0)
        while len(label_ids) < max_seq_length:
            label_ids.append(0)
            label_mask.append(0)
        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length
        assert len(label_ids) == max_seq_length
        assert len(valid) == max_seq_length
        assert len(label_mask) == max_seq_length

        if ex_index < 1:
            logger.info("*** Example ***")
            logger.info("guid: %s" % (example.guid))
            logger.info("tokens: %s" % " ".join(
                    [str(x) for x in tokens]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info("label_mask: %s" % " ".join([str(x) for x in label_mask]))
            logger.info(
                    "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info("label: %s (id = %s)" % (example.label, label_ids))

        features.append(
                InputFeatures(input_ids=input_ids,
                              input_mask=input_mask,
                              segment_ids=segment_ids,
                              label_id=label_ids,
                              valid_ids=valid,
                              label_mask=label_mask,
                              _id=example.guid))

        if ex_index % 1000 == 0:
            logger.info('converted {} / {} examples'.format(ex_index+1, len(examples)))

    logger.info('Done converting examples to features in {:.1f} minutes'.format((time.time() - s_time) / 60))
    return features


def _load_previous_best_score(previous_model_dir, dev_on, metric='f1_token'):
    previous_eval_files = glob.glob(os.path.join(previous_model_dir, "eval_results_{}_epoch*.json".format(dev_on)))
    best_score = -1
    for eval_file in previous_eval_files:
        try:
            cur_score = json.load(open(eval_file))[metric]
            if cur_score > best_score:
                best_score = cur_score
        except KeyError:
            continue
    return best_score


def \
        main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--bert_model", default=None, type=str, required=False,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                        "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                        "bert-base-multilingual-cased, bert-base-chinese.")

    parser.add_argument("--task_name",
                        default=None,
                        type=str,
                        required=True,
                        help="The name of the task to train.")
    # parser.add_argument("--output_dir",
    #                     default=None,
    #                     type=str,
    #                     required=True,
    #                     help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--cache_dir",
                        default="",
                        type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length",
                        default=128,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        action='store_true',
                        help="Whether to run eval or not.")
    parser.add_argument("--eval_on",
                        default="dev",
                        help="Whether to run eval on the dev set or test set.")

    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")

    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=4,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")

    parser.add_argument("--weight_decay", default=0.01, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")

    # additional ARGS
    # parser.add_argument("--early_stopping_patience",
    #                     type=int,
    #                     default=1,
    #                     help="early stopping patience")
    parser.add_argument("--train_on",
                        type=str,
                        help="Which dataset to use for training.")
    parser.add_argument("--dev_on",
                        type=str,
                        help="Which dataset to use for development.")

    parser.add_argument("--retrain_on",
                        type=str,
                        help="Which dataset to use for retraining.")

    parser.add_argument("--train_portion",
                        type=float,
                        default=1.0,
                        help="Portion of the training set to use.")

    parser.add_argument("--pretrained_model_id",
                        type=str,
                        help="Which model id to load for retraining.")

    parser.add_argument("--base_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The base dir (not output_dir).")

    parser.add_argument("--model_id",
                        default=None,
                        type=str,
                        required=True,
                        help="The model id.")

    parser.add_argument("--hidden_dropout_prob", default=0.1, type=float,
                        help="Hidden dropout prob.")

    parser.add_argument("--model_type", default='bert', type=str,
                        help="Model type (bert)") # unused

    args = parser.parse_args()

    pretrained_model_dir = os.path.join(args.base_dir, args.pretrained_model_id) if args.pretrained_model_id else None

    if args.retrain_on is not None:
        assert args.pretrained_model_id is not None
        # override config
        pretrained_model_args = json.load(open(os.path.join(pretrained_model_dir, "train_args.json")))

        args.bert_model = pretrained_model_args['bert_model']
        args.max_seq_length = pretrained_model_args['max_seq_length']
        args.do_lower_case = pretrained_model_args['do_lower_case']
        args.train_on = pretrained_model_args['train_on']

    output_dir = os.path.join(args.base_dir, args.model_id)
    if not args.bert_model and not args.do_train:
        args.bert_model = json.load(open(os.path.join(output_dir, "model_config.json")))['bert_model']

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    processors = {"ner": ConvSearchProcessor}

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    if os.path.exists(output_dir) and os.listdir(output_dir) and args.do_train:
        raise ValueError("Output directory ({}) already exists and is not empty.".format(output_dir))
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    task_name = args.task_name.lower()

    if task_name not in processors:
        raise ValueError("Task not found: %s" % (task_name))

    train_on = args.train_on if not args.retrain_on else args.retrain_on
    logger.info('Training on {}...'.format(train_on))
    processor = processors[task_name](train_on=train_on, dev_on=args.dev_on)

    label_list = processor.get_labels()
    num_labels = len(label_list) + 1

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)
    do_lower_case = args.do_lower_case

    writer = SummaryWriter('./runs/' + args.model_id)

    train_examples = None
    num_train_optimization_steps = 0
    if args.do_train:
        train_examples = processor.get_train_examples(args.data_dir, portion=args.train_portion,
                                                      uppercase=not do_lower_case)
        num_train_optimization_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps) * args.num_train_epochs
        if args.local_rank != -1:
            num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()

    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    if args.retrain_on is None:
        # Prepare a fresh model
        config = BertConfig.from_pretrained(args.bert_model,
                                            num_labels=num_labels,
                                            finetuning_task=args.task_name,
                                            hidden_dropout_prob=args.hidden_dropout_prob)

        model = Ner.from_pretrained(args.bert_model,
                                    from_tf=False,
                                    config=config)

    else:
        # resume a pretrained model!
        logger.info('Loading pretrained model {}..'.format(args.pretrained_model_id))
        model = Ner.from_pretrained(pretrained_model_dir, hidden_dropout_prob=args.hidden_dropout_prob)
        tokenizer = BertTokenizer.from_pretrained(pretrained_model_dir, do_lower_case=args.do_lower_case)
        best_f1_score = _load_previous_best_score(pretrained_model_dir, args.dev_on)
        logger.info('Loaded pretrained model {}. Prev best f1 score: {:.1f}'
                    .format(args.pretrained_model_id, 100*best_f1_score))


    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab


    #SELECT the device:cuda1
    #device = torch.device('cuda:1')

    model.to(device)
    logger.info('model in gpu?'+str(next(model.parameters()).is_cuda))

    param_optimizer = list(model.named_parameters())
    # print(param_optimizer)
    no_decay = ['bias', 'LayerNorm.weight']

    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer
                    if not any(nd in n for nd in no_decay) and not n == 'embedding.weight'],
         'weight_decay': args.weight_decay},

        {'params': [p for n, p in param_optimizer
                    if any(nd in n for nd in no_decay) and not n == 'embedding.weight'],
         'weight_decay': 0.0}
        ]

    warmup_steps = int(args.warmup_proportion * num_train_optimization_steps)
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = WarmupLinearSchedule(optimizer, warmup_steps=warmup_steps, t_total=num_train_optimization_steps)
    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if n_gpu > 1:
        model = torch.nn.DataParallel(model)

    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)

    global_step = 0
    epoch_i = -1
    total_nb_tr_steps = 0
    # tr_loss = 0
    # label_map = {i : label for i, label in enumerate(label_list,1)}

    if args.do_train:

        model.to(device)
        #logger.info("model in gpu"+str(next(model.parameters()).device))

        best_f1_score = -1.0
        train_features = convert_examples_to_features(
            train_examples, label_list, args.max_seq_length, tokenizer)
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)
        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_id for f in train_features], dtype=torch.long)
        all_valid_ids = torch.tensor([f.valid_ids for f in train_features], dtype=torch.long)
        all_lmask_ids = torch.tensor([f.label_mask for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids,all_valid_ids,all_lmask_ids)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        model.train()

        for epoch_i in trange(int(args.num_train_epochs), desc="Epoch"):
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            model.train()

            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids, valid_ids,l_mask = batch
                #input_ids = input_ids.to(device)
                #input_mask = input_mask.to(device)
                #segment_ids = segment_ids.to(device)
                #label_ids = label_ids.to(device)
                #valid_ids = valid_ids.to(device)
                #l_mask = l_mask.to(device)

                #logger.info('input_ids in gpu:'+str(input_ids.device))
                #logger.info('input_mask in gpu:'+str(input_mask.device))
                #logger.info('segment_ids in gpu:'+str(segment_ids.device))
                #logger.info('label_ids in gpu:'+str(label_ids.device))
                #logger.info('valid_ids in gpu:'+str(valid_ids.device))
                #logger.info('l_mask in gpu:'+str(l_mask.device))

                loss = model(input_ids, segment_ids, input_mask, label_ids,valid_ids,l_mask)
                if n_gpu > 1:
                    loss = loss.mean() # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                if args.fp16:
                    with amp.scale_loss(loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                loss_val = loss.item()
                # print('step: {} loss: {:.4f}'.format(nb_tr_steps, loss_val))

                tr_loss += loss_val
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                total_nb_tr_steps += 1

                if (total_nb_tr_steps + 1) % 10:
                    writer.add_scalar('Loss/train', loss_val, total_nb_tr_steps)

                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()
                    scheduler.step()  # Update learning rate schedule
                    model.zero_grad()
                    global_step += 1

            logger.info('[EPOCH {}] Training loss: {:.4f}'.format(epoch_i, tr_loss))
            cur_f1_score, cur__p_score, cur_r_score = _do_eval(args, epoch_i, device, processor, label_list, tokenizer, model, output_dir,
                                    args.max_seq_length, do_lower_case)

            writer.add_scalar('F1/dev', cur_f1_score, total_nb_tr_steps)
            writer.add_scalar('P/dev', cur__p_score, total_nb_tr_steps)
            writer.add_scalar('R/dev', cur_r_score, total_nb_tr_steps)

            if cur_f1_score > (best_f1_score + 0.001):
                # Save a trained model and the associated configuration
                model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self

                model_to_save.save_pretrained(output_dir)

                tokenizer.save_pretrained(output_dir)
                label_map = {i: label for i, label in enumerate(label_list,1)}
                model_config = {"bert_model": args.bert_model,
                                "do_lower": args.do_lower_case,
                                "max_seq_length": args.max_seq_length,
                                "num_labels": len(label_list)+1,
                                "label_map": label_map,
                                'hidden_dropout_prob': args.hidden_dropout_prob,
                                }

                json.dump(model_config, open(os.path.join(output_dir, "model_config.json"), "w"))

                d = args.__dict__
                d['epoch'] = epoch_i+1
                d['loss_train'] = tr_loss
                json.dump(d, open(os.path.join(output_dir, "train_args.json"), "w"))

                # Load a trained model and config that you have fine-tuned

                best_f1_score = cur_f1_score

            else:
                logger.info('F1 score did not improve ({:.2f} vs {:.2f}). Stopping...'.format(cur_f1_score, best_f1_score))
                break

    else:
        # Load a trained model and vocabulary that you have fine-tuned

        model = Ner.from_pretrained(output_dir)
        tokenizer = BertTokenizer.from_pretrained(output_dir, do_lower_case=args.do_lower_case)

    model.to(device)

    if args.do_eval and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        config_args = json.load(open(os.path.join(output_dir, "train_args.json")))
        max_seq_length = config_args['max_seq_length']
        _do_eval(args, epoch_i, device, processor, label_list, tokenizer, model, output_dir, max_seq_length, do_lower_case)

    writer.close()


def _do_eval(args, epoch_i, device, processor, label_list, tokenizer, model, output_dir, max_seq_length, do_lower_case):
    if args.eval_on == "dev":
        eval_examples = processor.get_dev_examples(args.data_dir, uppercase=not do_lower_case)
    elif args.eval_on == "test":
        eval_examples = processor.get_test_examples(args.data_dir, uppercase=not do_lower_case)
    else:
        raise ValueError("eval on dev or test set only")

    eval_features = convert_examples_to_features(eval_examples, label_list, max_seq_length, tokenizer)
    logger.info("***** Running evaluation *****")
    logger.info("  Num examples = %d", len(eval_examples))
    logger.info("  Batch size = %d", args.eval_batch_size)

    all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
    all_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.long)
    all_valid_ids = torch.tensor([f.valid_ids for f in eval_features], dtype=torch.long)
    all_lmask_ids = torch.tensor([f.label_mask for f in eval_features], dtype=torch.long)
    all_guids = [x.guid for x in eval_examples]
    eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids,all_valid_ids,all_lmask_ids)
    # Run prediction for full data
    eval_sampler = SequentialSampler(eval_data)
    eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)
    model.eval()
    # eval_loss, eval_accuracy = 0, 0
    # nb_eval_steps, nb_eval_examples = 0, 0
    y_true = []
    y_pred = []
    x_input = []
    _ids = []

    # whether to collapse multiple predictions of the same token in one
    label_map = {i : label for i, label in enumerate(label_list,1)}

    i_guids = 0
    for input_ids, input_mask, segment_ids, label_ids,valid_ids,l_mask in tqdm(eval_dataloader, desc="Evaluating"):
        input_ids = input_ids.to(device)
        input_mask = input_mask.to(device)
        segment_ids = segment_ids.to(device)
        valid_ids = valid_ids.to(device)
        label_ids = label_ids.to(device)
        l_mask = l_mask.to(device)

        with torch.no_grad():
            logits = model(input_ids, segment_ids, input_mask,valid_ids=valid_ids,attention_mask_label=l_mask)

        logits = torch.argmax(F.log_softmax(logits,dim=2),dim=2)
        logits = logits.detach().cpu().numpy()
        label_ids = label_ids.to('cpu').numpy()
        input_ids = input_ids.to('cpu').numpy()
        valid_ids = valid_ids.to('cpu').numpy()

        for i, label in enumerate(label_ids):
            temp_1 = []
            temp_2 = []
            temp_3 = []

            for j, m in enumerate(label):

                if j == 0:  # CLS
                    continue
                elif label_ids[i][j] == len(label_map):

                    tmp = tokenizer.convert_ids_to_tokens(input_ids[i])
                    x_input_tokens = []
                    for jj in range(1, len(tmp)):
                        token = tmp[jj]
                        if token == '[PAD]':
                            break
                        if valid_ids[i][jj] == 1:
                            x_input_tokens.append(token)
                        else:
                            x_input_tokens[-1] += token

                    # remove bert tokenization chars ## from tokens
                    x_input_tokens = [s.replace('##', '') for s in x_input_tokens]
                    x_input.append(x_input_tokens)

                    y_true.append(temp_1)
                    y_pred.append(temp_2)
                    _ids.append(all_guids[i_guids])
                    i_guids += 1

                    break
                else:
                    temp_1.append(label_map[label_ids[i][j]])
                    temp_2.append(label_map.get(logits[i][j], 'O'))
                    temp_3.append(input_ids[i][j])

    _f1_score_token = eval_seq_labeling_token.f1_score(y_true, y_pred, average='micro')
    _p_score_token = eval_seq_labeling_token.precision_score(y_true, y_pred, average='micro')
    _r_score_token = eval_seq_labeling_token.recall_score(y_true, y_pred, average='micro')

    logger.info('[Token eval] P={:.1f}, R={:.1f}, F1={:.1f}'.format(100 * _p_score_token, 100 * _r_score_token, 100 * _f1_score_token))

    ground_truth_file = os.path.join(args.data_dir, "{}.json".format(args.dev_on))

    d = {

        'f1_token': _f1_score_token,
        'precision_token': _p_score_token,
        'recall_token': _r_score_token,

        'y_true': y_true,
        'y_pred': y_pred,
        'x_input': x_input,
        'dev_on': args.dev_on,
        'ids': _ids
    }

    output_eval_file = os.path.join(output_dir, "eval_results_{}_epoch{}.json".format(args.dev_on, epoch_i+1))
    json.dump(d, open(output_eval_file, 'w'))

    return _f1_score_token, _p_score_token, _r_score_token


if __name__ == "__main__":
    main()
