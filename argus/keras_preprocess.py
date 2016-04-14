#!/usr/bin/python3
"""
Script used for training on argus anssel dataset.
Prerequisites:
    * Get glove.6B.50d.txt from http://nlp.stanford.edu/projects/glove/
"""
from __future__ import division
from __future__ import print_function

import importlib
import pickle

import keras.preprocessing.sequence as prep
import numpy as np
from keras.callbacks import ModelCheckpoint
from keras.layers.core import Activation
from keras.layers.core import Dropout, TimeDistributedDense
from keras.layers.embeddings import Embedding
from keras.models import Graph

import pysts.embedding as emb
import pysts.kerasts.blocks as B
import pysts.nlp as nlp
from argus.keras_layers import Reshape_, WeightedMean
from pysts.hyperparam import hash_params
from pysts.vocab import Vocabulary

s0pad = 60
s1pad = 60


# Used when tokenizing words
sentence_re = r'''(?x)      # set flag to allow verbose regexps
      (?:[A-Z])(?:\.[A-Z])+\.?  # abbreviations, e.g. U.S.A.
    | \w+(?:-\w+)*            # words with optional internal hyphens
    | \$?\d+(?:\.\d+)?%?      # currency and percentages, e.g. $12.40, 82%
    | \.\.\.                # ellipsis
    | [][.,;"'?():-_`]      # these are separate tokens
'''

import nltk
def tokenize(string):
    return nltk.regexp_tokenize(string, sentence_re)


class Q:
    def __init__(self, qtext, q, s, c, r, y):
        self.qtext = qtext  # str of question
        self.q = q  # list of one repeated tokenized question
        self.s = s  # list of tokenized sentences
        self.c = c  # class features, shape = (nb_sentences, w_dim)
        self.r = r  # rel features, shape = (nb_sentences, q_dim)
        self.y = y  # GS scalar


def load_sets(qs, max_sentences, vocab=None):
    # s0=questions, s1=sentences
    if vocab is None:
        s0, s1 = [], []
        for q in qs:
            s0 += q.q
            s1 += q.s
        vocab = Vocabulary(s0 + s1)
    si03d, si13d, f04d, f14d = [], [], [], []
    q_texts = []
    for q in qs:
        q_texts.append(q.qtext)
        s0 = q.q
        s1 = q.s
        si0 = vocab.vectorize(s0, spad=s0pad)
        si1 = vocab.vectorize(s1, spad=s1pad)
        si0 = prep.pad_sequences(si0.T, maxlen=max_sentences, padding='post', truncating='post').T
        si1 = prep.pad_sequences(si1.T, maxlen=max_sentences, padding='post', truncating='post').T
        si03d.append(si0)
        si13d.append(si1)

        f0, f1 = nlp.sentence_flags(s0, s1, s0pad, s1pad)
        f0 = prep.pad_sequences(f0.transpose((1, 0, 2)), maxlen=max_sentences, padding='post',
                                truncating='post', dtype='bool').transpose((1, 0, 2))
        f1 = prep.pad_sequences(f1.transpose((1, 0, 2)), maxlen=max_sentences, padding='post',
                                truncating='post', dtype='bool').transpose((1, 0, 2))
        f04d.append(f0)
        f14d.append(f1)

    # ==========================================
    c = np.array([prep.pad_sequences(q.c.T, maxlen=max_sentences, padding='post',
                                     truncating='post', dtype='float32') for q in qs])
    r = np.array([prep.pad_sequences(q.r.T, maxlen=max_sentences, padding='post',
                                     truncating='post', dtype='float32') for q in qs])
    c_in = c.transpose((0, 2, 1))
    r_in = r.transpose((0, 2, 1))
    y = np.array([q.y for q in qs])

    gr = {'si03d': np.array(si03d), 'si13d': np.array(si13d),
          'c_in': c_in, 'r_in': r_in, 'score': y, 'q_texts': q_texts}
    if f0 is not None:
        gr['f04d'] = np.array(f04d)
        gr['f14d'] = np.array(f14d)

    # print('print from gr:')
    # print(gr['si03d'][0], gr['si13d'][0])
    # sys.exit()
    return y, vocab, gr


def config(module_config, params):
    c = dict()
    c['embdim'] = 50
    c['inp_e_dropout'] = 0.
    c['e_add_flags'] = True

    c['ptscorer'] = B.mlp_ptscorer
    c['mlpsum'] = 'sum'
    c['Ddim'] = .1

    c['loss'] = 'binary_crossentropy'
    c['nb_epoch'] = 100

    c['class_mode'] = 'binary'
    module_config(c)

    for p in params:
        k, v = p.split('=')
        c[k] = eval(v)

    ps, h = hash_params(c)
    return c, ps, h


def prep_model(model, glove, vocab, module_prep_model, c, oact, s0pad, s1pad):
    # Input embedding and encoding
    N = embedding(model, glove, vocab, s0pad, s1pad, c['inp_e_dropout'], add_flags=c['e_add_flags'])

    # Sentence-aggregate embeddings
    final_outputs = module_prep_model(model, N, s0pad, s1pad, c)

    # model.add_node(name='scoreS1', inputs=final_outputs, merge_mode='concat',
    #                layer=Dense(output_dim=1, W_regularizer=l2(c['l2reg'])))
    #
    # model.add_node(name='scoreS2', inputs=final_outputs, merge_mode='concat',
    #                layer=Dense(output_dim=1, W_regularizer=l2(c['l2reg'])))

    # kwargs = dict()
    # kwargs['sum_mode'] = c['mlpsum']
    model.add_node(name='scoreS1', input=B.mlp_ptscorer(model, final_outputs, c['Ddim'], N, c['l2reg'], pfx='S1_'),
                   layer=Activation(oact))
    model.add_node(name='scoreS2', input=B.mlp_ptscorer(model, final_outputs, c['Ddim'], N, c['l2reg'], pfx='S2_'),
                   layer=Activation(oact))


def build_model(model, glove, vocab, module_prep_model, c, s0pad=s0pad, s1pad=s1pad):
    oact = 'linear'

    prep_model(model, glove, vocab, module_prep_model, c, oact, s0pad, s1pad)


def embedding(model, glove, vocab, s0pad, s1pad, dropout, trainable=False,
              add_flags=False):
    """ Sts embedding layer, without creating inputs. """

    if add_flags:
        outputs = ['e0[0]', 'e1[0]']
    else:
        outputs = ['e0', 'e1']

    model.add_shared_node(name='emb', inputs=['si0', 'si1'], outputs=outputs,
                          layer=Embedding(input_dim=vocab.size(), input_length=s1pad,
                                          output_dim=glove.N, mask_zero=True,
                                          weights=[vocab.embmatrix(glove)], trainable=trainable))
    if add_flags:
        for m in [0, 1]:
            model.add_node(name='e%d'%(m,), inputs=['e%d[0]'%(m,), 'f%d'%(m,)], merge_mode='concat', layer=Activation('linear'))
        N = glove.N + nlp.flagsdim
    else:
        N = glove.N

    model.add_shared_node(name='embdrop', inputs=['e0', 'e1'], outputs=['e0_', 'e1_'],
                          layer=Dropout(dropout, input_shape=(N,)))

    return N


def load_weights(model, filepath_rnn, filepath_clr):
    '''Load weights from a HDF5 file.
    '''
    import h5py

    f_rnn = h5py.File(filepath_rnn, mode='r')
    f_clr = h5py.File(filepath_clr, mode='r')
    g_rnn = f_rnn['graph']
    g_clr = f_clr['graph']
    w_rnn = [g_rnn['param_{}'.format(p)] for p in range(g_rnn.attrs['nb_params'])]
    w_clr = [g_clr['param_{}'.format(p)] for p in range(g_clr.attrs['nb_params'])]
    w_clr = [np.random.normal(0.,.01,x.shape) for x in w_clr]
    # w_clr[0] = np.random.normal(0.,.01,(21,))
    w_clr[0] = np.array([w_clr[0][9]]+list(w_clr[0][:10])+list(w_clr[0][10:]))
    w = w_rnn + w_clr
    print([x.shape for x in w])
    model.set_weights(w)
    f_rnn.close()
    f_clr.close()

c_r_out = []
features_outs = []
def build(w_dim, q_dim, max_sentences, optimizer, glove, vocab, module_prep_model, c):
    rnn_dim = 1
    w_full_dim = w_dim + rnn_dim
    q_full_dim = q_dim + rnn_dim
    print('Model')
    model = Graph()
    # ===================== inputs of size (batch_size, max_sentences, s_pad)
    model.add_input('si03d', (max_sentences, s0pad), dtype=int)  # XXX: cannot be cast to int->problem?
    model.add_input('si13d', (max_sentences, s1pad), dtype=int)
    if True:  # TODO: if flags
        model.add_input('f04d', (max_sentences, s0pad, nlp.flagsdim))
        model.add_input('f14d', (max_sentences, s1pad, nlp.flagsdim))
        model.add_node(Reshape_((s0pad, nlp.flagsdim)), 'f0', input='f04d')
        model.add_node(Reshape_((s1pad, nlp.flagsdim)), 'f1', input='f14d')

    # ===================== reshape to (batch_size * max_sentences, s_pad)
    model.add_node(Reshape_((s0pad,)), 'si0', input='si03d')
    model.add_node(Reshape_((s1pad,)), 'si1', input='si13d')

    # ===================== outputs from sts
    build_model(model, glove, vocab, module_prep_model, c)  # out = ['scoreS1', 'scoreS2']
    # ===================== reshape (batch_size * max_sentences,) -> (batch_size, max_sentences, 1)
    model.add_node(Reshape_((max_sentences, rnn_dim)), 'sts_in1', input='scoreS1')
    model.add_node(Reshape_((max_sentences, rnn_dim)), 'sts_in2', input='scoreS2')

    # ===================== connect sts outputs to c and r inputs
    model.add_input('c_in', (max_sentences, w_dim))
    model.add_input('r_in', (max_sentences, q_dim))
    model.add_node(Activation('linear'), 'c_full', inputs=['c_in', 'sts_in1'],
                   merge_mode='concat', concat_axis=-1)
    model.add_node(Activation('linear'), 'r_full', inputs=['r_in', 'sts_in2'],
                   merge_mode='concat', concat_axis=-1)
    # ===================== [w_full_dim, q_full_dim] -> [class, rel]
    model.add_node(TimeDistributedDense(1, activation='sigmoid'), 'c', input='c_full')
    model.add_node(TimeDistributedDense(1, activation='sigmoid'), 'r', input='r_full')
    model.add_node(Activation('linear'), 'c_r', inputs=['c', 'r'],
                   merge_mode='concat', concat_axis=-1)
    # ===================== mean of class over rel
    model.add_node(WeightedMean(w_dim=w_full_dim,
                                q_dim=q_full_dim,
                                max_sentences=max_sentences), name='weighted_mean', input='c_r')
    model.add_output(name='score', input='weighted_mean')

    model.compile(optimizer=optimizer, loss={'score': 'binary_crossentropy'})
    global c_r_out, features_outs
    c_r_out = layer_fun(model, 'c_r')
    features_outs = [layer_fun(model, 'c_full'), layer_fun(model, 'r_full')]
    # TODO: use for printing sts_outs
    return model

import theano
def layer_fun(model, layer_name):
    thf = theano.function([model.inputs[name].input for name in model.input_order],
                          model.nodes[layer_name].get_output(train=False),
                          on_unused_input='ignore', allow_input_downcast=True)
    return thf
    # return thf(*[gr[name] for name in model.input_order])


def load_and_train(runid, module_prep_model, c, glove, vocab, gr, grv, grt,
                   max_sentences, w_dim, q_dim, optimizer='sgd', test_path=None):

    model = build(w_dim, q_dim, max_sentences, optimizer, glove, vocab, module_prep_model, c)

    if test_path is None:
        print('Training')
        model.fit(gr, validation_data=grv,
                  callbacks=[ModelCheckpoint('weights-'+runid+'-bestval.h5',
                                             save_best_only=True, monitor='val_loss', mode='min')],
                  batch_size=10, nb_epoch=c['nb_epoch'], show_accuracy=True)
        model.save_weights('weights-'+runid+'-final.h5', overwrite=True)
        model.load_weights('weights-'+runid+'-bestval.h5')
    else:
        model.load_weights(test_path)

    return model


def load_model(model_path, vocab_path, w_dim, q_dim, max_sentences):
    from train import params
    print('Building model')
    epochs = 50
    optimizer = 'sgd'
    model_name = 'rnn'
    module = importlib.import_module('.'+model_name, 'models')
    conf, ps, h = config(module.config, params, epochs)

    glove = emb.GloVe(N=conf['embdim'])
    vocab = pickle.load(open(vocab_path))
    model = build(w_dim, q_dim, max_sentences, optimizer,
                  glove, vocab, module.prep_model, conf)
    model.load_weights(model_path)
    return model, vocab


if __name__ == '__main__':
    load_weights(0, 'sources/models/keras_model.h5', 'clr_model.h5')
