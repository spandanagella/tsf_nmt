# -*- coding: utf-8 -*-
"""
    Sequence-to-sequence model with bi-directional encoder and the attention mechanism described in

        arxiv.org/abs/1412.2007

    and support to buckets.

"""
import copy
import random
import numpy
import tensorflow as tf
# from tensorflow.models.rnn import rnn_cell, seq2seq
from tensorflow.models.rnn import rnn_cell, seq2seq, rnn
from tensorflow.python.ops import nn_ops, embedding_ops  #, rnn
from tensorflow.python.framework import ops
import data_utils
import attention
import cells

import sys

# from six.moves import xrange


def _reverse_encoder(source,
                    src_embedding,
                    encoder_cell,
                    batch_size,
                    dtype=tf.float32):
    """

    Parameters
    ----------
    source
    src_embedding
    encoder_cell
    batch_size
    dtype

    Returns
    -------

    """
    # get the embeddings
    with ops.device("/cpu:0"):
        emb_inp = [embedding_ops.embedding_lookup(src_embedding, s) for s in source]

    initial_state = encoder_cell.zero_state(batch_size=batch_size, dtype=dtype)

    outputs, states = rnn.rnn(encoder_cell, emb_inp,
                              initial_state=initial_state,
                              dtype=dtype,
                              scope='reverse_encoder')

    hidden_states = outputs

    decoder_initial_state = states[-1]

    return hidden_states, decoder_initial_state


def _decode(target,
            decoder_cell,
            decoder_initial_state,
            attention_states,
            target_vocab_size,
            output_projection,
            batch_size,
            do_decode=False,
            input_feeding=False,
            attention_type=None,
            content_function='vinyals_kayser',
            output_attention=False,
            dtype=tf.float32):
    assert attention_type is not None

    assert attention_type is 'local' or attention_type is 'global' or attention_type is 'hybrid'

    # decoder with attention
    with tf.name_scope('decoder_with_attention') as scope:
        # run the decoder with attention
        outputs, states = attention.embedding_attention_decoder(
                target, decoder_initial_state, attention_states,
                decoder_cell, batch_size, target_vocab_size,
                output_size=None, output_projection=output_projection,
                feed_previous=do_decode, input_feeding=input_feeding,
                attention_type=attention_type, dtype=dtype,
                content_function=content_function,
                output_attention=output_attention,
                scope='decoder_with_attention'
        )

    return outputs, states


def _get_optimizer(name='sgd', lr_rate=0.1, decay=1e-4):
    """

    Parameters
    ----------
    name
    lr_rate
    decay

    Returns
    -------

    """
    optimizer = None
    if name is 'sgd':
        optimizer = tf.train.GradientDescentOptimizer(lr_rate)
    elif name is 'adagrad':
        optimizer = tf.train.AdagradOptimizer(lr_rate)
    elif name is 'adam':
        optimizer = tf.train.AdamOptimizer(lr_rate)
    elif name is 'rmsprop':
        optimizer = tf.train.RMSPropOptimizer(lr_rate, decay)
    else:
        raise ValueError('Optimizer not found.')
    return optimizer


def _build_multicell_rnn(num_layers_encoder, num_layers_decoder, encoder_size, decoder_size,
                         source_proj_size, target_proj_size, use_lstm=True, input_feeding=True,
                         dropout=0.0):

    if use_lstm:
        cell_class = rnn_cell.LSTMCell
    else:
        cell_class = cells.GRU

    encoder_cell = cell_class(num_units=encoder_size, input_size=source_proj_size)
    if input_feeding:
        decoder_cell0 = cell_class(num_units=decoder_size, input_size=decoder_size * 2)
    else:
        decoder_cell0 = cell_class(num_units=decoder_size, input_size=decoder_size)
    decoder_cell1 = cell_class(num_units=decoder_size, input_size=decoder_size)

    if dropout > 0.0:  # if dropout is 0.0, it is turned off
        encoder_cell = rnn_cell.DropoutWrapper(encoder_cell, output_keep_prob=1.0-dropout)
        decoder_cell0 = rnn_cell.DropoutWrapper(decoder_cell0, output_keep_prob=1.0-dropout)
        decoder_cell1 = rnn_cell.DropoutWrapper(decoder_cell1, output_keep_prob=1.0-dropout)

    encoder_rnncell = rnn_cell.MultiRNNCell([encoder_cell] * num_layers_encoder)
    decoder_rnncell = rnn_cell.MultiRNNCell([decoder_cell0] + [decoder_cell1] * (num_layers_decoder - 1))

    return encoder_rnncell, decoder_rnncell


class Seq2SeqModel(object):
    """Sequence-to-sequence model with attention and for multiple buckets.
    This class implements a multi-layer recurrent neural network as encoder,
    and an attention-based decoder. This is the same as the model described in
    this paper: http://arxiv.org/abs/1412.7449 - please look there for details,
    or into the seq2seq library for complete model implementation.
    This class also allows to use GRU cells in addition to LSTM cells, and
    sampled softmax to handle large output vocabulary size. A single-layer
    version of this model, but with bi-directional encoder, was presented in
      http://arxiv.org/abs/1409.0473
    and sampled softmax is described in Section 3 of the following paper.
      http://arxiv.org/pdf/1412.2007v2.pdf
    """

    def __init__(self,
                 source_vocab_size,
                 target_vocab_size,
                 buckets,
                 source_proj_size,
                 target_proj_size,
                 encoder_size,
                 decoder_size,
                 num_layers_encoder,
                 num_layers_decoder,
                 max_gradient_norm,
                 batch_size,
                 learning_rate,
                 learning_rate_decay_factor,
                 optimizer='sgd',
                 use_lstm=False,
                 input_feeding=False,
                 dropout=0.0,
                 attention_type='global',
                 content_function='vinyals_kayser',
                 num_samples=512,
                 forward_only=False,
                 max_len=100,
                 cpu_only=False,
                 output_attention=False,
                 dtype=tf.float32):
        """Create the model.
        Args:
          source_vocab_size: size of the source vocabulary.
          target_vocab_size: size of the target vocabulary.
          buckets: a list of pairs (I, O), where I specifies maximum input length
            that will be processed in that bucket, and O specifies maximum output
            length. Training instances that have inputs longer than I or outputs
            longer than O will be pushed to the next bucket and padded accordingly.
            We assume that the list is sorted, e.g., [(2, 4), (8, 16)].
          size: number of units in each layer of the model.
          num_layers_encoder: number of layers in the model.
          max_gradient_norm: gradients will be clipped to maximally this norm.
          batch_size: the size of the batches used during training;
            the model construction is independent of batch_size, so it can be
            changed after initialization if this is convenient, e.g., for decoding.
          learning_rate: learning rate to start with.
          learning_rate_decay_factor: decay learning rate by this much when needed.
          use_lstm: if true, we use LSTM cells instead of GRU cells.
          num_samples: number of samples for sampled softmax.
          forward_only: if set, we do not construct the backward pass in the model.
        """
        if cpu_only:
            device = "/cpu:0"
        else:
            device = "/gpu:0"

        with tf.device(device):

            self.source_vocab_size = source_vocab_size
            self.target_vocab_size = target_vocab_size
            self.buckets = buckets
            self.batch_size = batch_size
            self.attention_type = attention_type
            self.content_function = content_function

            # learning rate ops
            self.learning_rate = tf.Variable(float(learning_rate), trainable=False)
            self.learning_rate_decay_op = self.learning_rate.assign(self.learning_rate * learning_rate_decay_factor)

            # epoch ops
            self.epoch = tf.Variable(0, trainable=False)
            self.epoch_update_op = self.epoch.assign(self.epoch + 1)

            # samples seen ops
            self.samples_seen = tf.Variable(0, trainable=False)
            self.samples_seen_update_op = self.samples_seen.assign(self.samples_seen + batch_size)
            self.samples_seen_reset_op = self.samples_seen.assign(0)

            # global step variable - controled by the model
            self.global_step = tf.Variable(0.0, trainable=False)

            # average loss ops
            self.current_loss = tf.Variable(0.0, trainable=False)
            self.current_loss_update_op = None
            self.avg_loss = tf.Variable(0.0, trainable=False)
            self.avg_loss_update_op = self.avg_loss.assign(tf.div(self.current_loss, self.global_step))

            self.source_proj_size = source_proj_size
            self.target_proj_size = target_proj_size
            self.encoder_size = encoder_size
            self.decoder_size = decoder_size

            self.input_feeding = input_feeding
            self.output_attention = output_attention
            self.max_len = max_len
            self.dropout = dropout

            self.dtype = dtype

            # If we use sampled softmax, we need an output projection.
            self.output_projection = None
            softmax_loss_function = None

            # Sampled softmax only makes sense if we sample less than vocabulary size.
            if 0 < num_samples < self.target_vocab_size:
                with tf.device("/cpu:0"):
                    w = tf.get_variable("proj_w", [decoder_size, self.target_vocab_size])
                    w_t = tf.transpose(w)
                    b = tf.get_variable("proj_b", [self.target_vocab_size])
                self.output_projection = (w, b)

                def sampled_loss(inputs, labels):
                    with tf.device("/cpu:0"):
                        labels = tf.reshape(labels, [-1, 1])
                        return tf.nn.sampled_softmax_loss(w_t, b, inputs, labels, num_samples,
                                                          self.target_vocab_size)

                softmax_loss_function = sampled_loss

            # create the embedding matrix - this must be done in the CPU for now
            with tf.device("/cpu:0"):
                self.src_embedding = tf.Variable(
                        tf.truncated_normal(
                                [source_vocab_size, source_proj_size], stddev=0.01
                        ),
                        name='embedding_src'
                )

                # decoder with attention
                with tf.name_scope('decoder_with_attention') as scope:
                    # create this variable to be used inside the embedding_attention_decoder
                    self.tgt_embedding = tf.Variable(
                            tf.truncated_normal(
                                    [target_vocab_size, target_proj_size], stddev=0.01
                            ),
                            name='embedding'
                    )

            # Create the internal multi-layer cell for our RNN.
            self.encoder_cell, self.decoder_cell = _build_multicell_rnn(
                    num_layers_encoder, num_layers_decoder, encoder_size, decoder_size,
                    source_proj_size, target_proj_size, use_lstm=use_lstm, dropout=dropout)

            # The seq2seq function: we use embedding for the input and attention.
            def seq2seq_f(encoder_inputs, decoder_inputs, do_decode):
                return self.inference(encoder_inputs, decoder_inputs, do_decode)

            # Feeds for inputs.
            self.encoder_inputs = []
            self.decoder_inputs = []
            self.target_weights = []
            # dropout feed
            dropout_feed = tf.placeholder(tf.float32, name="dropout_feed")

            for i in xrange(buckets[-1][0]):  # Last bucket is the biggest one.
                self.encoder_inputs.append(tf.placeholder(tf.int32, shape=[None], name="encoder{0}".format(i)))

            for i in xrange(buckets[-1][1] + 1):
                self.decoder_inputs.append(tf.placeholder(tf.int32, shape=[None, ], name="decoder{0}".format(i)))
                self.target_weights.append(tf.placeholder(tf.float32, shape=[None], name="weight{0}".format(i)))

            # Our targets are decoder inputs shifted by one.
            targets = [self.decoder_inputs[i + 1]
                       for i in xrange(len(self.decoder_inputs) - 1)]

            # Training outputs and losses.
            if forward_only:

                # self.outputs, self.losses = self.translation_inference(self.encoder_inputs)

                for i in xrange(len(self.encoder_inputs), self.max_len):
                    self.encoder_inputs.append(tf.placeholder(tf.int32, shape=[None], name="encoder{0}".format(i)))

                # context, decoder_initial_state, attention_states, input_length
                self.ret0, self.ret1, self.ret2 = self.encode(self.encoder_inputs)

                # shape of this placeholder: the first None indicate the batch size and the second the input length
                self.attn_plcholder = tf.placeholder(tf.float32,
                                                     shape=[None, self.ret2.get_shape()[1], target_proj_size],
                                                     name="attention_states")
                self.decoder_init_plcholder = tf.placeholder(tf.float32,
                                                             shape=[None, (target_proj_size) * 2 * num_layers_decoder],
                                                             name="decoder_init")

                self.logits, self.states = _decode([self.decoder_inputs[0]], self.decoder_cell, self.decoder_init_plcholder,
                                                   self.attn_plcholder, self.target_vocab_size, self.output_projection,
                                                   batch_size=self.batch_size, attention_type=self.attention_type,
                                                   content_function=self.content_function, do_decode=True,
                                                   input_feeding=self.input_feeding, dtype=self.dtype,
                                                   output_attention=self.output_attention)

                # If we use output projection, we need to project outputs for decoding.
                if self.output_projection is not None:
                    self.logits = [tf.nn.xw_plus_b(logit, self.output_projection[0], self.output_projection[1])
                                   for logit in self.logits]
                    self.logits =[nn_ops.softmax(logit) for logit in self.logits]

            else:
                self.outputs, self.losses = seq2seq.model_with_buckets(
                        self.encoder_inputs, self.decoder_inputs, targets,
                        self.target_weights, buckets, self.target_vocab_size,
                        lambda x, y: seq2seq_f(x, y, False),
                        softmax_loss_function=softmax_loss_function)

            # Gradients and SGD update operation for training the model.
            params = tf.trainable_variables()
            if not forward_only:
                self.gradient_norms = []
                self.updates = []
                # opt = tf.train.GradientDescentOptimizer(self.learning_rate)
                opt = _get_optimizer(optimizer, learning_rate)
                for b in xrange(len(buckets)):
                    gradients = tf.gradients(self.losses[b], params)
                    clipped_gradients, norm = tf.clip_by_global_norm(gradients,
                                                                     max_gradient_norm)
                    self.gradient_norms.append(norm)
                    self.updates.append(opt.apply_gradients(
                            zip(clipped_gradients, params), global_step=self.global_step))

            self.saver = tf.train.Saver(tf.all_variables())

    def inference(self, source, target, do_decode=False):
        """
        Function to be used together with the 'model_with_buckets' function from Tensorflow's
            seq2seq module.

        Parameters
        ----------
        source: Tensor
            a Tensor corresponding to the source sentence
        target: Tensor
            A Tensor corresponding to the target sentence
        do_decode: boolean
            Flag indicating whether or not to use the feed_previous parameter of the
                seq2seq.embedding_attention_decoder function.

        Returns
        -------

        """
        # encode source
        context, decoder_initial_state, attention_states = self.encode(source)
        # decode target
        outputs, states = _decode(target, self.decoder_cell, decoder_initial_state, attention_states,
                                  self.target_vocab_size, self.output_projection,
                                  batch_size=self.batch_size, attention_type=self.attention_type,
                                  do_decode=do_decode, input_feeding=self.input_feeding,
                                  content_function=self.content_function, dtype=self.dtype,
                                  output_attention=self.output_attention)

        # return the output (logits) and internal states
        return outputs, states

    def encode(self, source, translate=False):

        # encoder embedding layer and recurrent layer
        # with tf.name_scope('bidirectional_encoder') as scope:
        with tf.name_scope('reverse_encoder') as scope:
            if translate:
                scope.reuse_variables()
            context, decoder_initial_state = _reverse_encoder(
                    source, self.src_embedding, self.encoder_cell,
                    self.batch_size, dtype=self.dtype)

            # First calculate a concatenation of encoder outputs to put attention on.
            top_states = [
                tf.reshape(e, [-1, 1, self.encoder_size]) for e in context
                ]
            attention_states = tf.concat(1, top_states)

        return context, decoder_initial_state, attention_states

    def get_train_batch(self, data, bucket_id):
        """Get a random batch of data from the specified bucket, prepare for step.
        To feed data in step(..) it must be a list of batch-major vectors, while
        data here contains single length-major cases. So the main logic of this
        function is to re-index data cases to be in the proper format for feeding.
        Args:
          data: a tuple of size len(self.buckets) in which each element contains
            lists of pairs of input and output data that we use to create a batch.
          bucket_id: integer, which bucket to get the batch for.
        Returns:
          The triple (encoder_inputs, decoder_inputs, target_weights) for
          the constructed batch that has the proper format to call step(...) later.
        """
        encoder_size, decoder_size = self.buckets[bucket_id]
        encoder_inputs, decoder_inputs = [], []

        n_target_words = 0

        # Get a random batch of encoder and decoder inputs from data,
        # pad them if needed, reverse encoder inputs and add GO to decoder.
        for _ in xrange(self.batch_size):
            d = data[bucket_id]
            # encoder_input, _, decoder_input = random.choice(d)
            encoder_input, decoder_input = random.choice(d)

            # Encoder inputs are padded and then reversed.
            encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
            encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))

            n_target_words += len(decoder_input)

            # Decoder inputs get an extra "GO" symbol, and are padded then.
            decoder_pad_size = decoder_size - len(decoder_input) - 1
            decoder_inputs.append([data_utils.GO_ID] + decoder_input +
                                  [data_utils.PAD_ID] * decoder_pad_size)

        # Now we create batch-major vectors from the data selected above.
        batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []

        # Batch encoder inputs are just re-indexed encoder_inputs.
        for length_idx in xrange(encoder_size):
            batch_encoder_inputs.append(
                    numpy.array([encoder_inputs[batch_idx][length_idx]
                              for batch_idx in xrange(self.batch_size)], dtype=numpy.int32))

        # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
        for length_idx in xrange(decoder_size):
            batch_decoder_inputs.append(
                    numpy.array([decoder_inputs[batch_idx][length_idx]
                              for batch_idx in xrange(self.batch_size)], dtype=numpy.int32))

            # Create target_weights to be 0 for targets that are padding.
            batch_weight = numpy.ones(self.batch_size, dtype=numpy.float32)
            for batch_idx in xrange(self.batch_size):
                # We set weight to 0 if the corresponding target is a PAD symbol.
                # The corresponding target is decoder_input shifted by 1 forward.
                if length_idx < decoder_size - 1:
                    target = decoder_inputs[batch_idx][length_idx + 1]
                if length_idx == decoder_size - 1 or target == data_utils.PAD_ID:
                    batch_weight[batch_idx] = 0.0
            batch_weights.append(batch_weight)

        return batch_encoder_inputs, batch_decoder_inputs, batch_weights, n_target_words

    def train_step(self, session, encoder_inputs, decoder_inputs, target_weights, bucket_id):
        """Run a step of the model feeding the given inputs.
        Args:
          session: tensorflow session to use.
          encoder_inputs: list of numpy int vectors to feed as encoder inputs.
          decoder_inputs: list of numpy int vectors to feed as decoder inputs.
          target_weights: list of numpy float vectors to feed as target weights.
          bucket_id: which bucket of the model to use.
          forward_only: whether to do the backward step or only forward.
          softmax: whether to apply softmax to the output_logits before returning them
        Returns:
          A triple consisting of gradient norm (or None if we did not do backward),
          average perplexity, and the outputs.
        Raises:
          ValueError: if length of enconder_inputs, decoder_inputs, or
            target_weights disagrees with bucket size for the specified bucket_id.
        """
        # Check if the sizes match.
        encoder_size, decoder_size = self.buckets[bucket_id]
        if len(encoder_inputs) != encoder_size:
            raise ValueError("Encoder length must be equal to the one in bucket,"
                             " %d != %d." % (len(encoder_inputs), encoder_size))
        if len(decoder_inputs) != decoder_size:
            raise ValueError("Decoder length must be equal to the one in bucket,"
                             " %d != %d." % (len(decoder_inputs), decoder_size))
        if len(target_weights) != decoder_size:
            raise ValueError("Weights length must be equal to the one in bucket,"
                             " %d != %d." % (len(target_weights), decoder_size))

        # Input feed: encoder inputs, decoder inputs, target_weights, as provided.
        input_feed = {}
        for l in xrange(encoder_size):
            input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]
        for l in xrange(decoder_size):
            input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
            input_feed[self.target_weights[l].name] = target_weights[l]

        # Since our targets are decoder inputs shifted by one, we need one more.
        last_target = self.decoder_inputs[decoder_size].name
        input_feed[last_target] = numpy.zeros([self.batch_size], dtype=numpy.int32)

        # Output feed: depends on whether we do a backward step or not.
        output_feed = [self.updates[bucket_id],  # Update Op that does SGD.
                       self.gradient_norms[bucket_id],  # Gradient norm.
                       self.losses[bucket_id]]  # Loss for this batch.

        outputs = session.run(output_feed, feed_dict=input_feed)
        return None, outputs[0], outputs[1:]  # No gradient norm, loss, outputs.

    def get_translate_batch(self, data):
        """Get a random batch of data from the specified bucket, prepare for step.
        To feed data in step(..) it must be a list of batch-major vectors, while
        data here contains single length-major cases. So the main logic of this
        function is to re-index data cases to be in the proper format for feeding.
        Args:
          data: a tuple of size len(self.buckets) in which each element contains
            lists of pairs of input and output data that we use to create a batch.
          bucket_id: integer, which bucket to get the batch for.
        Returns:
          The triple (encoder_inputs, decoder_inputs, target_weights) for
          the constructed batch that has the proper format to call step(...) later.
        """
        encoder_size, decoder_size = (self.max_len, 1)
        encoder_inputs, decoder_inputs = [], []

        # Get a random batch of encoder and decoder inputs from data,
        # pad them if needed, reverse encoder inputs and add GO to decoder.
        for _ in xrange(self.batch_size):
            # encoder_input, _, decoder_input = random.choice(d)
            encoder_input, decoder_input = random.choice(data)

            # Encoder inputs are padded and then reversed.
            encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
            encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))

            # Decoder inputs get an extra "GO" symbol, and are padded then.
            decoder_pad_size = decoder_size - len(decoder_input) - 1
            decoder_inputs.append([data_utils.GO_ID] + decoder_input +
                                  [data_utils.PAD_ID] * decoder_pad_size)

        # Now we create batch-major vectors from the data selected above.
        batch_encoder_inputs, batch_decoder_inputs = [], []

        # Batch encoder inputs are just re-indexed encoder_inputs.
        for length_idx in xrange(encoder_size):
            batch_encoder_inputs.append(
                    numpy.array([encoder_inputs[batch_idx][length_idx]
                              for batch_idx in xrange(self.batch_size)], dtype=numpy.int32))

        # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
        for length_idx in xrange(decoder_size):
            batch_decoder_inputs.append(
                    numpy.array([decoder_inputs[batch_idx][length_idx]
                              for batch_idx in xrange(self.batch_size)], dtype=numpy.int32))

        return batch_encoder_inputs, batch_decoder_inputs

    def translation_step(self, session, token_ids, beam_size=5, normalize=True, dump_remaining=False):

        sample = []
        sample_score = []

        live_hyp = 1
        dead_hyp = 0

        hyp_samples = [[]] * live_hyp
        hyp_scores = numpy.zeros(live_hyp).astype('float32')

        # Get a 1-element batch to feed the sentence to the model
        encoder_inputs, decoder_inputs = self.get_translate_batch([(token_ids, [])])
        decoder_inputs = decoder_inputs[-1]

        # here we encode the input sentence
        encoder_input_feed = {}
        for l in xrange(self.max_len):
            encoder_input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]

        # we select the last element of ret0 to keep as it is a list of hidden_states
        encoder_output_feed = [self.ret0[-1], self.ret1, self.ret2]

        # get the return of encoding step: hidden_states, decoder_initial_states, attention_states
        ret = session.run(encoder_output_feed, encoder_input_feed)

        # here we get info to the decode step
        attention_states = ret[2]
        shape = ret[1][0].shape
        decoder_init =ret[1][0].reshape(1, shape[0])

        # we must retrieve the last state to feed the decoder run
        decoder_output_feed = [self.logits[-1], self.states[-1]]

        for ii in xrange(self.max_len):

            if ii > 0:
                pass

            # we must feed decoder_initial_state and attention_states to run one decode step
            decoder_input_feed = {self.decoder_inputs[0].name : decoder_inputs,
                                  self.decoder_init_plcholder.name: decoder_init,
                                  self.attn_plcholder.name: attention_states}

            ret = session.run(decoder_output_feed, decoder_input_feed)

            next_p = ret[0]
            next_state = ret[1]

            cand_scores = hyp_scores[:, None] - numpy.log(next_p)
            cand_flat = cand_scores.flatten()
            ranks_flat = cand_flat.argsort()[:(beam_size-dead_hyp)]

            voc_size = next_p.shape[1]
            trans_indices = ranks_flat / voc_size
            word_indices = ranks_flat % voc_size
            costs = cand_flat[ranks_flat]

            new_hyp_samples = []
            new_hyp_scores = numpy.zeros(beam_size-dead_hyp).astype('float32')
            new_hyp_states = []

            for idx, [ti, wi] in enumerate(zip(trans_indices, word_indices)):
                new_hyp_samples.append(hyp_samples[ti]+[wi])
                new_hyp_scores[idx] = copy.copy(costs[ti])
                new_hyp_states.append(copy.copy(next_state[ti]))

            # check the finished samples
            new_live_k = 0
            hyp_samples = []
            hyp_scores = []
            hyp_states = []

            for idx in xrange(len(new_hyp_samples)):
                if new_hyp_samples[idx][-1] == data_utils.EOS_ID:
                    sample.append(new_hyp_samples[idx])
                    sample_score.append(new_hyp_scores[idx])
                    dead_hyp += 1
                else:
                    new_live_k += 1
                    hyp_samples.append(new_hyp_samples[idx])
                    hyp_scores.append(new_hyp_scores[idx])
                    hyp_states.append(new_hyp_states[idx])
            hyp_scores = numpy.array(hyp_scores)
            live_hyp = new_live_k

            if new_live_k < 1:
                break
            if dead_hyp >= beam_size:
                break

            decoder_inputs = numpy.array([w[-1] for w in hyp_samples])
            decoder_init = numpy.array(hyp_states)

        # dump every remaining one
        if dump_remaining:
            if live_hyp > 0:
                for idx in xrange(live_hyp):
                    sample.append(hyp_samples[idx])
                    sample_score.append(hyp_scores[idx])

        # normalize scores according to sequence lengths
        if normalize:
            lengths = numpy.array([len(s) for s in sample])
            sample_score = sample_score / lengths

        # sort the samples by score (it is in log-scale, therefore lower is better)
        sidx = numpy.argsort(sample_score)
        sample = numpy.array(sample)[sidx]
        sample_score = numpy.array(sample_score)[sidx]

        return sample.tolist(), sample_score.tolist()
