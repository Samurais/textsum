# coding:utf-8
import tensorflow as tf
from tensorflow.python.client import timeline
import sys
import logging
import numpy as np
import time
import math
import os

import data_utils
from seq2seqmodel import Seq2SeqModel
from data_iterator import DataIterator

############################
######## MARK:FLAGS ########
############################

# model
tf.app.flags.DEFINE_string("mode", "TRAIN", "TRAIN|BEAM_DECODE")

# datasets, paths, and preprocessing
tf.app.flags.DEFINE_string("model_dir", "./model", "model_dir/data_cache/n model_dir/saved_model; model_dir/log.txt .")
tf.app.flags.DEFINE_string("train_path_from", "./train", "the absolute path of raw source train file.")
tf.app.flags.DEFINE_string("dev_path_from", "./dev", "the absolute path of raw source dev file.")
tf.app.flags.DEFINE_string("test_path_from", "./test", "the absolute path of raw source test file.")

tf.app.flags.DEFINE_string("train_path_to", "./train", "the absolute path of raw target train file.")
tf.app.flags.DEFINE_string("dev_path_to", "./dev", "the absolute path of raw target dev file.")
tf.app.flags.DEFINE_string("test_path_to", "./test", "the absolute path of raw target test file.")

tf.app.flags.DEFINE_string("decode_output", "./output", "beam search decode output.")

# tuning hypers
tf.app.flags.DEFINE_float("learning_rate", 0.5, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.83,"Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,"Clip gradients to this norm.")
tf.app.flags.DEFINE_float("keep_prob", 0.5, "dropout rate.")
tf.app.flags.DEFINE_integer("batch_size", 64,"Batch size to use during training/evaluation.")

tf.app.flags.DEFINE_integer("from_vocab_size", 10000, "from vocabulary size.")
tf.app.flags.DEFINE_integer("to_vocab_size", 10000, "to vocabulary size.")

tf.app.flags.DEFINE_integer("size", 128, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 2, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("n_epoch", 500,"Maximum number of epochs in training.")

tf.app.flags.DEFINE_integer("n_bucket", 10,"num of buckets to run.")
tf.app.flags.DEFINE_integer("patience", 10, "exit if the model can't improve for $patence evals")

# devices
tf.app.flags.DEFINE_string("N", "000", "GPU layer distribution: [input_embedding, lstm, output_embedding]")

# training parameter
tf.app.flags.DEFINE_boolean("withAdagrad", True,"withAdagrad.")
tf.app.flags.DEFINE_boolean("fromScratch", True,"withAdagrad.")
tf.app.flags.DEFINE_boolean("saveCheckpoint", False, "save Model at each checkpoint.")
tf.app.flags.DEFINE_boolean("profile", False, "False = no profile, True = profile")

# for beam_decode
tf.app.flags.DEFINE_integer("beam_size", 10,"the beam size")
tf.app.flags.DEFINE_boolean("print_beam", True, "to print beam info")
tf.app.flags.DEFINE_float("min_ratio", 0.5, "min_ratio.")
tf.app.flags.DEFINE_float("max_ratio", 1.5, "max_ratio.")

# GPU configuration
tf.app.flags.DEFINE_boolean("allow_growth", False, "allow growth")

# Summary
tf.app.flags.DEFINE_boolean("with_summary", False, "with_summary")

# With Attention
tf.app.flags.DEFINE_boolean("attention", False, "with_attention")

FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
_buckets =buckets = [(120, 30), (200, 35), (300, 40), (400, 41), (500, 42)]
_beam_buckets = [120,200,300,400,500]



#日志函数
def mylog(msg):
    print(msg)
    sys.stdout.flush()
    logging.info(msg)

def mylog_section(section_name):
    mylog("======== {} ========".format(section_name))

def mylog_line(section_name, message):
    mylog("[{}] {}".format(section_name, message))

def log_flags():
    members = FLAGS.__dict__['__flags'].keys()
    mylog_section("FLAGS")
    for attr in members:
        mylog("{}={}".format(attr, getattr(FLAGS, attr)))



#读数据
def read_data(source_path, target_path, max_size=None):
  """Read data from source and target files and put into buckets.
  Args:
    source_path: path to the files with token-ids for the source language.
    target_path: path to the file with token-ids for the target language;
      it must be aligned with the source file: n-th line contains the desired
      output for n-th line from the source_path.
    max_size: maximum number of lines to read, all other will be ignored;
      if 0 or None, data files will be read completely (no limit).
  Returns:
    data_set: a list of length len(_buckets); data_set[n] contains a list of
      (source, target) pairs read from the provided data files that fit
      into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
      len(target) < _buckets[n][1]; source and target are lists of token-ids.
  """
  data_set = [[] for _ in _buckets]
  with tf.gfile.GFile(source_path, mode="r") as source_file:
    with tf.gfile.GFile(target_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target and (not max_size or counter < max_size):
        counter += 1
        if counter % 100000 == 0:
          print("  reading data line %d" % counter)
          sys.stdout.flush()
        source_ids = [int(x) for x in source.split()][::-1]
        target_ids = [int(x) for x in target.split()]
        target_ids.append(data_utils.EOS_ID)
        for bucket_id, (source_size, target_size) in enumerate(_buckets):
          if len(source_ids) < source_size and len(target_ids) < target_size:
            data_set[bucket_id].append([source_ids, target_ids])
            break
        source, target = source_file.readline(), target_file.readline()
  return data_set

#创建模型
def get_device_address(s):
    add = []
    if s == "":
        for i in xrange(3):
            add.append("/cpu:0")
    else:
        add = ["/gpu:{}".format(int(x)) for x in s]

    return add

def create_model(session, run_options, run_metadata):
    devices = get_device_address(FLAGS.N)
    dtype = tf.float32
    model = Seq2SeqModel(FLAGS._buckets,
                     FLAGS.size,
                     FLAGS.real_vocab_size_from,
                     FLAGS.real_vocab_size_to,
                     FLAGS.num_layers,
                     FLAGS.max_gradient_norm,
                     FLAGS.batch_size,
                     FLAGS.learning_rate,
                     FLAGS.learning_rate_decay_factor,
                     withAdagrad = FLAGS.withAdagrad,
                     dropoutRate = FLAGS.keep_prob,
                     dtype = dtype,
                     devices = devices,
                     topk_n = FLAGS.beam_size,
                     run_options = run_options,
                     run_metadata = run_metadata,
                     with_attention = FLAGS.attention,
                     beam_search = FLAGS.beam_search,
                     beam_buckets = _beam_buckets
                     )

    ckpt = tf.train.get_checkpoint_state(FLAGS.saved_model_dir)
    # if FLAGS.recommend or (not FLAGS.fromScratch) and ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):

    if FLAGS.mode == "BEAM_DECODE"  or (not FLAGS.fromScratch) and ckpt:

        mylog("Reading model parameters from %s" % ckpt.model_checkpoint_path)
        model.saver.restore(session, ckpt.model_checkpoint_path)
    else:
        mylog("Created model with fresh parameters.")
        session.run(tf.global_variables_initializer())
    return model
#输出数据
def show_all_variables():
    all_vars = tf.global_variables()
    for var in all_vars:
        mylog(var.name)

#模型评价
def evaluate(sess, model, data_set):
    # Run evals on development set and print their perplexity/loss.
    dropoutRateRaw = FLAGS.keep_prob
    sess.run(model.dropout10_op)

    start_id = 0
    loss = 0.0
    n_steps = 0
    n_valids = 0
    batch_size = FLAGS.batch_size

    dite = DataIterator(model, data_set, len(FLAGS._buckets), batch_size, None)
    ite = dite.next_sequence(stop=True)

    for sources, inputs, outputs, weights, bucket_id in ite:
        L = model.step(sess, sources, inputs, outputs, weights, bucket_id, forward_only=True)
        loss += L
        n_steps += 1
        n_valids += np.sum(weights)

    loss = loss / (n_valids)
    ppx = math.exp(loss) if loss < 300 else float("inf")

    sess.run(model.dropoutAssign_op)

    return loss, ppx

def train():
    # Read Data
    mylog_section("READ DATA")

    from_train = None
    to_train = None
    from_dev = None
    to_dev = None

    #提取文本摘要,在创建词汇表过程中,from和to都应该合并统计
    from_train, to_train, from_dev, to_dev, _, _ = data_utils.prepare_data(
        FLAGS.data_cache_dir,
        FLAGS.train_path_from,
        FLAGS.train_path_to,
        FLAGS.dev_path_from,
        FLAGS.dev_path_to,
        FLAGS.from_vocab_size,
        FLAGS.to_vocab_size)

    train_data_bucket = read_data(from_train, to_train)
    dev_data_bucket = read_data(from_dev, to_dev)
    _, _, real_vocab_size_from, real_vocab_size_to = data_utils.get_vocab_info(FLAGS.data_cache_dir)

    FLAGS._buckets = _buckets
    FLAGS.real_vocab_size_from = real_vocab_size_from
    FLAGS.real_vocab_size_to = real_vocab_size_to

    # train_n_tokens = total training target size
    train_n_tokens = np.sum([np.sum([len(items[1]) for items in x]) for x in train_data_bucket])
    train_bucket_sizes = [len(train_data_bucket[b]) for b in xrange(len(_buckets))]
    train_total_size = float(sum(train_bucket_sizes))
    train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size for i in xrange(len(train_bucket_sizes))]
    dev_bucket_sizes = [len(dev_data_bucket[b]) for b in xrange(len(_buckets))]
    dev_total_size = int(sum(dev_bucket_sizes))

    mylog_section("REPORT")
    # steps
    batch_size = FLAGS.batch_size
    n_epoch = FLAGS.n_epoch
    steps_per_epoch = int(train_total_size / batch_size)
    steps_per_dev = int(dev_total_size / batch_size)
    steps_per_checkpoint = int(steps_per_epoch / 2)
    total_steps = steps_per_epoch * n_epoch

    # reports
    mylog("from_vocab_size: {}".format(FLAGS.from_vocab_size))
    mylog("to_vocab_size: {}".format(FLAGS.to_vocab_size))
    mylog("_buckets: {}".format(FLAGS._buckets))
    mylog("Train:")
    mylog("total: {}".format(train_total_size))
    mylog("bucket sizes: {}".format(train_bucket_sizes))
    mylog("Dev:")
    mylog("total: {}".format(dev_total_size))
    mylog("bucket sizes: {}".format(dev_bucket_sizes))
    mylog("Steps_per_epoch: {}".format(steps_per_epoch))
    mylog("Total_steps:{}".format(total_steps))
    mylog("Steps_per_checkpoint: {}".format(steps_per_checkpoint))

    mylog_section("IN TENSORFLOW")

    config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
    config.gpu_options.allow_growth = FLAGS.allow_growth

    with tf.Session(config=config) as sess:

        # runtime profile
        if FLAGS.profile:
            run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()
        else:
            run_options = None
            run_metadata = None

        mylog_section("MODEL/SUMMARY/WRITER")

        mylog("Creating Model.. (this can take a few minutes)")
        model = create_model(sess, run_options, run_metadata)

        mylog_section("All Variables")
        show_all_variables()

        # Data Iterators
        mylog_section("Data Iterators")

        dite = DataIterator(model, train_data_bucket, len(train_buckets_scale), batch_size, train_buckets_scale)

        iteType = 0
        ite=None
        if iteType == 0:
            mylog("Itetype: withRandom")
            ite = dite.next_random()
        elif iteType == 1:
            mylog("Itetype: withSequence")
            ite = dite.next_sequence()

        # statistics during training
        step_time, loss = 0.0, 0.0
        current_step = 0
        previous_losses = []
        low_ppx = float("inf")
        low_ppx_step = 0
        steps_per_report = 30
        n_targets_report = 0
        report_time = 0
        n_valid_sents = 0
        n_valid_words = 0
        patience = FLAGS.patience

        mylog_section("TRAIN")

        while current_step < total_steps:

            # start
            start_time = time.time()

            # data and train
            source_inputs, target_inputs, target_outputs, target_weights, bucket_id = ite.next()

            L = model.step(sess, source_inputs, target_inputs, target_outputs, target_weights, bucket_id)

            # loss and time
            step_time += (time.time() - start_time) / steps_per_checkpoint

            loss += L
            current_step += 1
            n_valid_sents += np.sum(np.sign(target_weights[0]))
            n_valid_words += np.sum(target_weights)

            # for report
            report_time += (time.time() - start_time)
            n_targets_report += np.sum(target_weights)

            if current_step % steps_per_report == 0:
                sect_name = "STEP {}".format(current_step)
                msg = "StepTime: {:.2f} sec Speed: {:.2f} targets/s Total_targets: {}".format(
                    report_time / steps_per_report, n_targets_report * 1.0 / report_time, train_n_tokens)
                mylog_line(sect_name, msg)

                report_time = 0
                n_targets_report = 0

                # Create the Timeline object, and write it to a json
                if FLAGS.profile:
                    tl = timeline.Timeline(run_metadata.step_stats)
                    ctf = tl.generate_chrome_trace_format()
                    with open('timeline.json', 'w') as f:
                        f.write(ctf)
                    exit()

            if current_step % steps_per_checkpoint == 0:

                i_checkpoint = int(current_step / steps_per_checkpoint)

                # train_ppx
                loss = loss / n_valid_words
                train_ppx = math.exp(float(loss)) if loss < 300 else float("inf")
                learning_rate = model.learning_rate.eval()

                # dev_ppx
                dev_loss, dev_ppx = evaluate(sess, model, dev_data_bucket)

                # report
                sect_name = "CHECKPOINT {} STEP {}".format(i_checkpoint, current_step)
                msg = "Learning_rate: {:.4f} Dev_ppx: {:.2f} Train_ppx: {:.2f}".format(learning_rate, dev_ppx, train_ppx)
                mylog_line(sect_name, msg)


                # save model per checkpoint
                if FLAGS.saveCheckpoint:
                    checkpoint_path = os.path.join(FLAGS.saved_model_dir, "model")
                    s = time.time()
                    model.saver.save(sess, checkpoint_path, global_step=i_checkpoint, write_meta_graph=False)
                    msg = "Model saved using {:.2f} sec at {}".format(time.time() - s, checkpoint_path)
                    mylog_line(sect_name, msg)

                # save best model
                if dev_ppx < low_ppx:
                    patience = FLAGS.patience
                    low_ppx = dev_ppx
                    low_ppx_step = current_step
                    checkpoint_path = os.path.join(FLAGS.saved_model_dir, "best")
                    s = time.time()
                    model.best_saver.save(sess, checkpoint_path, global_step=0, write_meta_graph=False)
                    msg = "Model saved using {:.2f} sec at {}".format(time.time() - s, checkpoint_path)
                    mylog_line(sect_name, msg)
                else:
                    patience -= 1

                if patience <= 0:
                    mylog("Training finished. Running out of patience.")
                    break

                # Save checkpoint and zero timer and loss.
                step_time, loss, n_valid_sents, n_valid_words = 0.0, 0.0, 0, 0


#参数函数
def mkdir(path):
    if not os.path.exists(path):
        os.mkdir(path)

def parsing_flags():
    # saved_model

    FLAGS.data_cache_dir = os.path.join(FLAGS.model_dir, "data_cache")
    FLAGS.saved_model_dir = os.path.join(FLAGS.model_dir, "saved_model")
    FLAGS.summary_dir = FLAGS.saved_model_dir

    mkdir(FLAGS.model_dir)
    mkdir(FLAGS.data_cache_dir)
    mkdir(FLAGS.saved_model_dir)
    mkdir(FLAGS.summary_dir)

    # for logs
    log_path = os.path.join(FLAGS.model_dir, "log.{}.txt".format(FLAGS.mode))
    filemode = 'w' if FLAGS.fromScratch else "a"
    logging.basicConfig(filename=log_path, level=logging.DEBUG, filemode=filemode)

    FLAGS.beam_search = False

    log_flags()

def main():
    parsing_flags()
    if FLAGS.mode == "TRAIN":
        train()
    else:
        print ('This function for train!!')
if __name__=='__main__':
    tf.app.run(main=main())