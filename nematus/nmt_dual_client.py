#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Build a neural machine translation model with soft attention
"""

import copy
import os
import sys
import time
import logging
from copy import deepcopy

import Pyro4
import numpy
from theano.tensor.shared_randomstreams import RandomStreams

from data_iterator import TextIterator, MonoIterator
from nmt_client import default_model_options, pred_probs
from nmt_utils import prepare_data, gen_sample
from pyro_utils import setup_remotes, get_random_key, get_unused_port
from util import load_dict

profile = False
bypass_pyro = False  # True
LOCALMODELDIR = '' # TODO: add language model directory


def _add_dim(x_pre):
    # add an extra dimension, as expected on x input to prepare_data
    # TODO: assuming no factors!
    xx = []
    for sent in x_pre:
        xx.append([[wid, ] for wid in sent])
    return xx


def _train_foo(remote_mt, _xxx, _yyy, _per_sent_weight, _lrate, maxlen):
    _x_prep, _x_mask, _y_prep, _y_mask = prepare_data(_add_dim(_xxx), _yyy, maxlen=maxlen)

    if _x_prep is None:
        logging.warn('_x_prep is None')
        return None

    if _x_mask is None:
        logging.warn('_x_mask is None')
        return None

    if _y_prep is None:
        logging.warn('_y_prep is None')
        return None

    if _y_mask is None:
        logging.warn('_y_mask is None')
        return None

    try:
        logging.debug('_xxx shape: %s, type=%s', numpy.shape(_xxx), type(_xxx))
        logging.debug('_yyy shape: %s, type=%s', numpy.shape(_yyy), type(_yyy))
        logging.debug('_per_sent_weight shape: %s, type=%s', numpy.shape(_per_sent_weight), type(_per_sent_weight))
        logging.debug('maxlen=%s', maxlen)
        logging.debug('_x_prep shape: %s, type=%s', numpy.shape(_x_prep), type(_x_prep))
        logging.debug('_x_mask shape: %s, type=%s', numpy.shape(_x_mask), type(_x_mask))
        logging.debug('_y_prep shape: %s, type=%s', numpy.shape(_y_prep), type(_y_prep))
        logging.debug('_y_mask shape: %s, type=%s', numpy.shape(_y_mask), type(_y_mask))
    except:
        logging.warn('logging shapes/types failed!')

    if len(_xxx) != len(_yyy) != len(_per_sent_weight):
        raise Exception('lengths of _xxx, _yyy, and/or _per_sent_weight do not match')

    if len(_xxx) != numpy.shape(_x_prep)[-1]:
        logging.warn('--BAD--    '*40 + 'I DO NOT KNOW WHY, BUT SOMETIMES prepare_data() DECIDES TO THROW SENTENCE AWAY!! TODO!! figure out what is going on. skipping.')
        return None

    remote_mt.set_noise_val(0.)
    # returns cost, which is related to log probs BUT may be weighted per sentence, and may include regularization terms!
    cost = remote_mt.x_f_grad_shared(_x_prep, _x_mask, _y_prep, _y_mask, _per_sent_weight, per_sent_cost=True)
    remote_mt.x_f_update(_lrate)  # TODO: WAIT TILL END?
    # check for bad numbers, usually we remove non-finite elements
    # and continue training - but not done here
    if any(numpy.isnan(cost)) or any(numpy.isinf(cost)):
        raise Exception('NaN detected')
    # TODO: this is wasteful! save time and compute at same time as cost above
    per_sent_neg_log_prob = remote_mt.x_f_log_probs(_x_prep, _x_mask, _y_prep, _y_mask, )
    # log(prob) is negative; higher is better, i.e. this is a reward
    # -log(prob) is positivel smaller is better, i.e. this is a cost
    # scale by -1. to get back to a reward
    per_sent_mt_reward = -1.0 * per_sent_neg_log_prob
    return per_sent_mt_reward


def monolingual_train(mt_systems, lm_1,
                      data, trng, k, maxlen,
                      worddicts_r, worddicts,
                      alpha, learning_rate_big,
                      learning_rate_small):

    logging.info('monolingual_train called')

    mt_01, mt_10 = mt_systems
    num2word_01, num2word_10 = worddicts_r
    word2num_01, word2num_10 = worddicts

    # Keeps a track of how many clean translations were retained for
    # each source sentence.
    per_sent_translation_count = []
    batch_sents1_01_clean = []
    batch_sents0_01_clean = []
    batch_per_trans_r1 = []

    for sent_ii, sent in enumerate(data):
        try:
            logging.debug('sent 0 #%d: %s', sent_ii, ' '.join([num2word_01[0][foo[0]] for foo in sent]))
        except:
            logging.error('could not print sent 0')

        # TRANSLATE 0->1
        sents1_01, scores_1, _, _, _ = gen_sample([mt_01.x_f_init],
                                                  [mt_01.x_f_next],
                                                  numpy.array([sent, ]),
                                                  trng=trng, k=k,
                                                  maxlen=maxlen,
                                                  stochastic=False,
                                                  argmax=False,
                                                  suppress_unk=True,
                                                  return_hyp_graph=False)

        try:
            for ii, sent1_01 in enumerate(sents1_01):
                logging.debug('sent 0->1 #%d (in system 01 vocab): %s',
                              ii, ' '.join([num2word_01[1][foo] for foo in sent1_01]))
        except:
            logging.error('failed to print sent 0->1 sentences (in system 01 vocab)')

        # strip out <eos>, </s> tags (I have no idea where </s> is coming from!)
        sents1_01_tmp = []
        for sent_1 in sents1_01:
            sents1_01_tmp.append([x for x in sent_1 if num2word_01[1][x] not in ('<eos>', '</s>')])
        sents1_01 = sents1_01_tmp

        # Clean Data (for length - translated sentence may not be acceptable length)
        # There's a variable number of translations per sentence in the beam now.
        # Keep track of that.
        sents1_01_clean = []
        sents0_01_clean = []
        for ii, sent_1 in enumerate(sents1_01):
            if len(sent_1) < 2:
                logging.debug('len(sent1 #%d)=%d, < 2. skipping', ii, len(sent_1))
            elif len(sent_1) >= maxlen:
                logging.debug('len(sent1 #%d)=%d, >= %d. skipping', ii, len(sent_1), maxlen)
            else:
                sents1_01_clean.append(sent_1)
                sents0_01_clean.append([x[0] for x in sent])

        if len(sents1_01_clean) == 0:
            logging.info("No acceptable length data out of 0->1 system from sent0 #%d", sent_ii)
            continue

        # Keep track of how many translations were retained
        # This is messy
        # [2, 2, 3, 3, 3, 1, ...]
        for _ in range(len(sents1_01_clean)):
            per_sent_translation_count.append(len(sents1_01_clean))

        # Add the clean translations to a list to track
        batch_sents1_01_clean += sents1_01_clean # list extend
        batch_sents0_01_clean += sents0_01_clean # list extend

        # LANGUAGE MODEL SCORE IN LANG 1
        # This will return a per-translation reward; it's a list of LM rewards
        r_1 = lm_1.score(numpy.array(sents1_01_clean).T)
        logging.debug("scores_lm1=%s", r_1)
        batch_per_trans_r1 += r_1 # list extend

    ###################### END of per-sent per-trans translations ################

    logging.debug('per_sent_translation_count=%s',per_sent_translation_count)
    logging.debug('batch_sents1_01_clean=%s',batch_sents1_01_clean)
    logging.debug('batch_sents0_01_clean=%s',batch_sents0_01_clean)
    logging.debug('batch_per_trans_r1=%s',batch_per_trans_r1)

    logging.debug('len per_sent_translation_count=%s',len(per_sent_translation_count))
    logging.debug('len batch_sents1_01_clean=%s',len(batch_sents1_01_clean))
    logging.debug('len batch_sents0_01_clean=%s',len(batch_sents0_01_clean))
    logging.debug('len batch_per_trans_r1=%s',len(batch_per_trans_r1))

    if len(batch_per_trans_r1) == 0:
        logging.warn('no acceptable length sentences found in entire batch. exiting function early')
        return

    # Make sure we got the book-keeping right
    assert len(per_sent_translation_count) == len(batch_sents1_01_clean)

    # The two MT systems have different vocabularies
    # Convert from mt01's vocab to mt10's vocab
    # for all translations for all sentences
    # each source sentence may have a variable number of translations from 0->1
    batch_sents1_10_clean = []
    for num1 in batch_sents1_01_clean:
        words = [num2word_01[1][num] for num in num1]
        words = [w for w in words if w not in ('<eos>', '</s>')]
        num2 = [word2num_10[0][word] for word in words]
        batch_sents1_10_clean.append(num2)

    try:
        #TODO(Gaurav): fix this logging to not print all translations for all sentences
        for ii, sent1_01 in enumerate(batch_sents1_10_clean):
            logging.debug('sent 0->1 #%d (in system 10 vocab): %s', 
                          ii, ' '.join([num2word_10[0][foo] for foo in sent1_01]) )
    except:
        logging.error('failed to print sent 0->1 sentences (in system 10 vocab)')


    # These are not translations from 1->0 but rather
    # the original source sentence in the vocab of the MT10 system
    batch_sents0_10_clean = []
    for num1 in batch_sents0_01_clean:
        words = [num2word_01[0][num] for num in num1]
        words = [w for w in words if w not in ('<eos>', '</s>')]
        num2 = [word2num_10[1][word] for word in words]
        batch_sents0_10_clean.append(num2)


    try:
        #TODO(Gaurav) : fix this logging to not print all source sentences in the MT10 vocab
        for ii, sent0_10 in enumerate(batch_sents0_10_clean):
            logging.debug('sent 0 #%d (in system 10 vocab): %s', 
                          ii, ' '.join([num2word_10[1][foo] for foo in sent0_10]))
    except:
        logging.error('failed to print sent 0 sentences (in system 10 vocab)')


    # MT 0->1 SCORE AND UPDATE
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        try:
            # TODO: Woah! This is a lot of stuff to print
            batch_sents0_10_for_debug, _, _, _, _ = gen_sample([mt_10.x_f_init],
                                                         [mt_10.x_f_next],
                                                         numpy.array([[[x, ] for x in batch_sents1_10_clean[0]], ]),
                                                         trng=trng, k=1,
                                                         maxlen=maxlen,
                                                         stochastic=False,
                                                         argmax=False,
                                                         suppress_unk=True,
                                                         return_hyp_graph=False)
            
            logging.debug('[just for degug] sentence 0->1->0 #0 (in system 10 vocab): %s', 
                          ' '.join([num2word_10[1][x] for x in batch_sents0_10_for_debug[0]]))
        except:
            logging.warning('failed to sample or print 0->1->0 sentences')


    per_sent_translation_count = numpy.array(per_sent_translation_count)
    batch_per_trans_r1 = numpy.array(batch_per_trans_r1)

    # Equation <todo>
    # /per_sent_translation_count == 1/K
    per_sent_weight = (1 - alpha) / per_sent_translation_count

    logging.info('psw10=%s', per_sent_weight)

    r_2 = _train_foo(mt_10, batch_sents1_10_clean, batch_sents0_10_clean,
                     per_sent_weight, learning_rate_big, maxlen)

    if r_2 is None:
        logging.warning('prepare_data() failed for some reason. returning early.')
        # code below will crash... TODO FIGURE OUT WHY THIS HAPPENS
        return 

    r_2 = numpy.array(r_2)

    logging.info('r_2=%s', r_2)


    # Equation <todo>
    # /per_sent_translation_count: 1/K
    # -1 because <todo>
    logging.debug('part 1: %s', (alpha * batch_per_trans_r1) )  # negative
    logging.debug('part 2: %s', (1 - alpha) * r_2 )             # negative
    per_sent_weight = np_res = -1 * (alpha * batch_per_trans_r1 + (1 - alpha) * r_2) / per_sent_translation_count

    logging.info('psw01=%s', per_sent_weight)

    final_r = _train_foo(mt_01, batch_sents0_10_clean, batch_sents1_10_clean,
                         per_sent_weight, learning_rate_small, maxlen)

    logging.info('final_r=%s', final_r)



def few_dict_items(a):
    return [(x, a[x]) for x in list(a)[:15]], 'len=%d'%len(a)


def check_model_options(model_options, dictionaries):
    if model_options['dim_per_factor'] is None:
        if model_options['factors'] == 1:
            model_options['dim_per_factor'] = [model_options['dim_word']]
        else:
            sys.stderr.write('Error: if using factored input, you must specify \'dim_per_factor\'\n')
            sys.exit(1)

    assert (len(dictionaries) == model_options['factors'] + 1)  # one dictionary per source factor + 1 for target factor
    assert (len(model_options['dim_per_factor']) == model_options[
        'factors'])  # each factor embedding has its own dimensionality
    assert (sum(model_options['dim_per_factor']) == model_options[
        'dim_word'])  # dimensionality of factor embeddings sums up to total dimensionality of input embedding vector

    if model_options['factors'] > 1:
        raise Exception('I probably broke factors...')


def reverse_model_options(model_options_a_b):
    model_options_b_a = copy.deepcopy(model_options_a_b)
    # for key in ['datasets', 'valid_datasets', 'domain_interpolation_indomain_datasets']:
    #    model_options_b_a[key] = model_options_a_b[key][::-1]
    model_options_b_a['saveto'] = model_options_a_b['saveto'] + '.BA'
    model_options_b_a['n_words_src'] = model_options_a_b['n_words']  # n_words is target
    model_options_b_a['n_words'] = model_options_a_b['n_words_src']
    return model_options_b_a


def train(**kwargs):
    if bypass_pyro:
        train2(**kwargs)
    else:
        current_script_dir = os.path.dirname(os.path.abspath(__file__))
        nmt_remote_script = os.path.join(current_script_dir, 'nmt_remote.py')
        lm_remote_script = os.path.join(current_script_dir, 'lm_remote.py')

        kwargs.update({'pyro_name_mt_a_b': 'mtAB',
                       'pyro_name_mt_b_a': 'mtBA',
                       'pyro_name_lm_a': 'lmA',
                       'pyro_name_lm_b': 'lmB',
                       'pyro_key': get_random_key(),
                       'pyro_port': get_unused_port(),
                       })

        with setup_remotes(
                remote_metadata_list=[dict(script=nmt_remote_script, name=kwargs['pyro_name_mt_a_b'], gpu_id=kwargs['mt_gpu_ids'][0]),
                                      dict(script=nmt_remote_script, name=kwargs['pyro_name_mt_b_a'], gpu_id=kwargs['mt_gpu_ids'][1]),
                                      dict(script=lm_remote_script, name=kwargs['pyro_name_lm_a'], gpu_id=kwargs['lm_gpu_ids'][0]),
                                      dict(script=lm_remote_script, name=kwargs['pyro_name_lm_b'], gpu_id=kwargs['lm_gpu_ids'][1])],
                pyro_port=kwargs['pyro_port'],
                pyro_key=kwargs['pyro_key']):
            train2(**kwargs)


# noinspection PyUnusedLocal
def train2(model_options_a_b=None,
           model_options_b_a=None,
           max_epochs=5000,
           finish_after=10000000,
           disp_freq=100,
           save_freq=1000,
           lrate=0.01,
           maxlen=100,
           batch_size=16,
           valid_batch_size=16,
           patience=10,
           parallel_datasets=(),
           valid_datasets=(),
           monolingual_datasets=(),
           dictionaries_a_b=None,
           dictionaries_b_a=None,
           #domain_interpolation_indomain_datasets=('indomain.en', 'indomain.fr'), # TODO unused
           valid_freq=1000,
           sample_freq=100,
           overwrite=False,
           external_validation_script=None,
           shuffle_each_epoch=True,
           sort_by_length=True,
           use_domain_interpolation=False,
           domain_interpolation_min=0.1,
           domain_interpolation_inc=0.1,
           maxibatch_size=20,
           pyro_key=None,
           pyro_port=None,
           pyro_name_mt_a_b=None,
           pyro_name_mt_b_a=None,
           pyro_name_lm_a=None,
           pyro_name_lm_b=None,
           language_models=(),
           mt_gpu_ids=(),
           lm_gpu_ids=(),
           ):

    if model_options_a_b is None:
        model_options_a_b = default_model_options

    if model_options_b_a is None:
        model_options_b_a = reverse_model_options(model_options_a_b)

    check_model_options(model_options_a_b, dictionaries_a_b)
    check_model_options(model_options_b_a, dictionaries_b_a)

    # I can't come up with a reason that trng must be shared... (I use a different seed)
    trng = RandomStreams(hash(__file__) % 4294967294)

    def create_worddicts_and_update_model_options(dictionaries, model_opts):
        # load dictionaries and invert them
        worddicts = [None] * len(dictionaries)
        worddicts_r = [None] * len(dictionaries)
        for k, dd in enumerate(dictionaries):
            worddicts[k] = load_dict(dd)
            worddicts_r[k] = dict()
            for kk, vv in worddicts[k].iteritems():
                worddicts_r[k][vv] = kk
            worddicts_r[k][0] = '<eos>'
            worddicts_r[k][1] = 'UNK'

        if model_opts['n_words_src'] is None:
            model_opts['n_words_src'] = len(worddicts[0])
        if model_opts['n_words'] is None:
            model_opts['n_words'] = len(worddicts[1])

        return worddicts, worddicts_r

    worddicts_a_b, worddicts_r_a_b = create_worddicts_and_update_model_options(dictionaries_a_b, model_options_a_b)
    worddicts_b_a, worddicts_r_b_a = create_worddicts_and_update_model_options(dictionaries_b_a, model_options_b_a)
    

    print '############################'
    print 'len(r_a_b)', len(worddicts_r_a_b),
    print 'r_a_b[0]', few_dict_items(worddicts_r_a_b[0]), '...'
    print 'r_a_b[1]', few_dict_items(worddicts_r_a_b[1]), '...'
    print 'r_b_a[0]', few_dict_items(worddicts_r_b_a[0]), '...'
    print 'r_b_a[1]', few_dict_items(worddicts_r_b_a[1]), '...'
    #print type(worddicts_a_b)
    #print 'len(a_b)', len(worddicts_a_b)


    def _load_data(dataset_a,
                   dataset_b,
                   valid_dataset_a,
                   valid_dataset_b,
                   dict_a,
                   dict_b,
                   model_opts):
        _train = TextIterator(dataset_a, dataset_b,
                              dict_a, dict_b,
                              n_words_source=model_opts['n_words_src'],
                              n_words_target=model_opts['n_words'],
                              batch_size=batch_size,
                              maxlen=maxlen,
                              skip_empty=True,
                              shuffle_each_epoch=shuffle_each_epoch,
                              sort_by_length=sort_by_length,
                              maxibatch_size=maxibatch_size)

        _valid = TextIterator(valid_dataset_a, valid_dataset_b,
                              dict_a, dict_b,
                              n_words_source=model_opts['n_words_src'],
                              n_words_target=model_opts['n_words'],
                              batch_size=valid_batch_size,
                              maxlen=maxlen)

        return _train, _valid

    def _load_mono_data(dataset,
                        dict_list,
                        model_opts):
        _train = MonoIterator(dataset,
                              dict_list,
                              n_words_source=model_opts['n_words_src'],
                              batch_size=batch_size,
                              maxlen=maxlen,
                              skip_empty=True,
                              shuffle_each_epoch=shuffle_each_epoch,
                              sort_by_length=sort_by_length,
                              maxibatch_size=maxibatch_size)

        return _train

    print 'Loading data'
    domain_interpolation_cur = None

    train_a_b, valid_a_b = _load_data(parallel_datasets[0], parallel_datasets[1],
                                      valid_datasets[0], valid_datasets[1],
                                      [dictionaries_a_b[0], ], dictionaries_a_b[1], model_options_a_b)  # TODO: why a list?
    train_b_a, valid_b_a, = _load_data(parallel_datasets[1], parallel_datasets[0],
                                       valid_datasets[1], valid_datasets[0],
                                       [dictionaries_b_a[0], ], dictionaries_b_a[1], model_options_b_a)  # TODO: why a list?

    train_a = _load_mono_data(monolingual_datasets[0], (dictionaries_a_b[0],), model_options_a_b)
    train_b = _load_mono_data(monolingual_datasets[1], (dictionaries_b_a[0],), model_options_b_a)

    def _data_generator(data_a_b, data_b_a, mono_a, mono_b):
        while True:
            ab_a, ab_b = data_a_b.next()
            ba_b, ba_a = data_b_a.next()
            a = mono_a.next()
            b = mono_b.next()
            yield 'mt', ((ab_a, ab_b), (ba_b, ba_a))
            yield 'mono-a', a  
            yield 'mono-b', b

    training = _data_generator(train_a_b, train_b_a, train_a, train_b)

    # In order to transfer numpy objects across the network, must use pickle as Pyro Serializer.
    # Also requires various environment flags (PYRO_SERIALIZERS_ACCEPTED, PYRO_SERIALIZER)
    #   for both name server and server.
    Pyro4.config.SERIALIZER = 'pickle'
    Pyro4.config.NS_PORT = pyro_port

    if (pyro_name_mt_a_b is not None) and (pyro_name_mt_b_a is not None):
        print 'Setting up remote translation engines'
        remote_mt_a_b = Pyro4.Proxy("PYRONAME:{0}".format(pyro_name_mt_a_b))
        remote_mt_a_b._pyroHmacKey = pyro_key
        remote_mt_b_a = Pyro4.Proxy("PYRONAME:{0}".format(pyro_name_mt_b_a))
        remote_mt_b_a._pyroHmacKey = pyro_key
    else:  # better for IDE
        print 'Importing translation engines'
        from nmt_remote import RemoteMT
        remote_mt_a_b = RemoteMT()
        remote_mt_b_a = RemoteMT()

    if (pyro_name_lm_a is not None) and (pyro_name_lm_b is not None):
        print 'Setting up remote language models'
        remote_lm_a = Pyro4.Proxy("PYRONAME:{0}".format(pyro_name_lm_a))
        remote_lm_a._pyroHmacKey = pyro_key
        remote_lm_b = Pyro4.Proxy("PYRONAME:{0}".format(pyro_name_lm_b))
        remote_lm_b._pyroHmacKey = pyro_key
    else:  # better for IDE
        print 'Importing language models'
        from lm_remote import RemoteLM
        remote_lm_a = RemoteLM()
        remote_lm_b = RemoteLM()

    print 'initializing remote MT0'
    remote_mt_a_b.init(model_options_a_b)
    print 'initializing remote MT1'
    # remote_mt_a_b.set_noise_val(0.5) # TEST: make sure it initilized
    remote_mt_b_a.init(model_options_b_a)
    print 'initializing remote LM0'
    remote_lm_a.init(language_models[0], worddicts_r_b_a[1])  # scoring going INTO language, so use A from BA
    print 'initializing remote LM1'
    remote_lm_b.init(language_models[1], worddicts_r_a_b[1])  # scoring going INTO language, so use B from AB

    # # setup asynchronous remote wrappers
    # remote_mtAB_async = Pyro4.async(remote_mt_a_b)
    # remote_mtBA_async = Pyro4.async(remote_mt_b_a)
    # remote_lmA_async = Pyro4.async(remote_lm_a)
    # remote_lmB_async = Pyro4.async(remote_lm_b)
    #
    # # asynchronously synchronize remotes
    # r0 = remote_mtAB_async.init(model_optionsAB)
    # r1 = remote_mtBA_async.init(model_optionsBA)
    # r2 = remote_lmA_async.init(language_models[0], worddicts_r[0])
    # r3 = remote_lmB_async.init(language_models[1], worddicts_r[1])
    # # synchronize
    # for x in [r0, r1, r2, r3]:
    #     _ = x.value

    print 'Remotes should be initilized'

    print 'Optimization'

    best_p = [None for _ in range(2)]
    bad_counter = 0
    # uidx = [0 for _ in range(2)] # TODO they should not be different in different models, use just one
    estop = False
    history_errs = [[] for _ in range(2)]

    for idx, model_options in zip(range(2), [model_options_a_b, model_options_b_a]):
        # reload history
        if model_options['reload_'] and os.path.exists(model_options['saveto']):
            rmodel = numpy.load(model_options['saveto'])
            history_errs[idx] = list(rmodel['history_errs'])
            # modify saveto so as not to overwrite original model
            #            model_options['saveto']=os.path.join(LOCALMODELDIR, basename(model_options['saveto']))
            # if 'uidx' in rmodel:
            #     uidx[idx] = rmodel['uidx']

            # save model options
            #        json.dump(model_options, open('%s.json' % model_options['saveto'], 'wb'), indent=2)

    if valid_freq == -1:
        valid_freq = len(training[0]) / batch_size
    if save_freq == -1:
        save_freq = len(training[0]) / batch_size
    if sample_freq == -1:
        sample_freq = len(training[0]) / batch_size

    valid_err = None

    last_disp_samples = 0
    ud_start = time.time()
    p_validation = None
    k = 2
    alpha = 0.05
    learning_rate_small = 0.0002 / batch_size  # gamma_1,t in paper, scaled by batch_size
    learning_rate_big = 0.02 / batch_size  # gamma_2,t in paper, scaled by batch_size
    for eidx in xrange(max_epochs):
        # n_samples = 0
        logging.info('epoch=%d', eidx)

        # validation
        # if valid_freq and numpy.mod(eidx, valid_freq) == 0:
        # TODO: for not, validating every epoch... 
        for _valid, _model_options, _remote_mt, _name in zip([valid_a_b,         valid_b_a        ],
                                                             [model_options_a_b, model_options_b_a],
                                                             [remote_mt_a_b,     remote_mt_b_a    ],
                                                             ['a->b',            'b->a'           ], ):
                _remote_mt.set_noise_val(0.)
                valid_errs, _ = pred_probs(_remote_mt.x_f_log_probs, prepare_data, _model_options, _valid, verbose=False)
                valid_err = valid_errs.mean()
                logging.info('epoch=%d, MT %s valid_err=%.1f', eidx, _name, valid_err)

        for data_type, data in training:

            if data_type == 'mt':
                logging.debug('training on bitext')

                for (x, y), model_options, _remote_mt in zip(data,
                                                             [model_options_a_b, model_options_b_a],
                                                             [remote_mt_a_b,     remote_mt_b_a    ]):

                    # ensure consistency in number of factors
                    if len(x) and len(x[0]) and len(x[0][0]) != model_options['factors']:
                        logging.exception('Error: mismatch between number of factors in settings ({0}), '
                                          'and number in training corpus ({1})\n'.format(model_options['factors'], len(x[0][0])))

                    # n_samples += len(x)  # TODO double count??
                    # last_disp_samples += len(x)
                    # # uidx += 1

                    # TODO: training each system in parallel!

                    x_prep, x_mask, y_prep, y_mask = prepare_data(x, y, maxlen=maxlen)

                    _remote_mt.set_noise_val(1.)

                    if x_prep is None:
                        logging.warning('x_prep is None')
                        # uidx -= 1
                        continue

                    cost = _remote_mt.x_f_grad_shared(x_prep, x_mask, y_prep, y_mask)

                    # check for bad numbers, usually we remove non-finite elements
                    # and continue training - but not done here
                    if numpy.isnan(cost) or numpy.isinf(cost):
                        logging.exception('NaN detected')

                    # do the update on parameters
                    _remote_mt.x_f_update(lrate)

            elif data_type == 'mono-a':
                logging.info('#'*40 + 'training the a -> b -> a loop.')
                monolingual_train([remote_mt_a_b, remote_mt_b_a],
                                  remote_lm_b, data, trng, k, maxlen,
                                  [worddicts_r_a_b, worddicts_r_b_a],
                                  [worddicts_a_b,   worddicts_b_a],
                                  alpha, learning_rate_big,
                                  learning_rate_small)
            elif data_type == 'mono-b':
                logging.info('#'*40 + 'training the b -> a -> b loop.')
                monolingual_train([remote_mt_b_a, remote_mt_a_b],
                                  remote_lm_a, data, trng, k, maxlen,
                                  [worddicts_r_b_a, worddicts_r_a_b],
                                  [worddicts_b_a,   worddicts_a_b],
                                  alpha, learning_rate_big,
                                  learning_rate_small)
            else:
                raise Exception('This should be unreachable. How did you get here?')

    return None
