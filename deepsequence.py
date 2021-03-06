"""
Deeply stacked sequences for bnas.
Can be both teacher-forced with scan, and single-stepped in beam search.
"""

from collections import namedtuple
import theano
from theano import tensor as T

from bnas import init
from bnas.model import *
from bnas.fun import function
from bnas.utils import expand_to_batch

Recurrence = namedtuple('Recurrence',
    ['variable', 'init', 'dropout'])
NonSequence = namedtuple('NonSequence',
    ['variable', 'func', 'idx'])
OutputOnly = object()

class Unit(Model):
    """Base class for recurrent units"""
    def __init__(self, name):
        super().__init__(name)
        # recurrent inputs/outputs
        self._recurrences = []
        # non-sequence inputs
        self._non_sequences = []

    def add_recurrence(self, var, init=None, dropout=0):
        """params:
            var -- Theano variable
            init -- 1) None if nontrainable init will be passed as input
                    2) parameter if a trainable init
                 or 3) OutputOnly if no init is needed
            dropout -- float in interval [0,1)
        """
        self._recurrences.append(Recurrence(var, init, dropout))

    def add_non_sequence(self, var, func=None, idx=None):
        """params:
            var -- Theano variable
            func -- 1) None if passed in as input
                 or 2) function(var) -> var
                       if precomputed from another input
            idx -- index of input non-sequences to give
                   as argument to func
        """
        if func is not None:
            assert idx is not None
        self._non_sequences.append(NonSequence(var, func, idx))

    @property
    def recurrences(self):
        return tuple(self._recurrences)

    @property
    def non_sequences(self):
        return tuple(self._non_sequences)

    @property
    def n_rec(self):
        return len(self.recurrences)

    @property
    def n_nonseq(self):
        return len(self.non_sequences)

    # subclasses should define step(out, unit_recs, unit_nonseqs) -> unit_recs

class DeepSequence(Model):
    """Recurrent sequence with one or more units"""
    def __init__(self, name, units, backwards=False, offset=0):
        super().__init__(name)
        self.units = units if units is not None else []
        for unit in self.units:
            self.add(unit)
        self.backwards = backwards
        self.offset = offset
        self._step_fun = None
        self._ns_func_cache = {}

    def __call__(self, inputs, inputs_mask,
                 nontrainable_recurrent_inits=None, non_sequences=None):
        batch_size = inputs.shape[1]
        inits_in = self.make_inits(
            nontrainable_recurrent_inits, batch_size, include_nones=True)
        seqs_in = [{'input': inputs, 'taps': [self.offset]},
                   {'input': inputs_mask, 'taps': [self.offset]}]
        # FIXME: add extra sequences, if needed
        non_sequences_in = self.make_nonsequences(non_sequences)
        seqs, _ = theano.scan(
                fn=self.step,
                go_backwards=self.backwards,
                sequences=seqs_in,
                outputs_info=inits_in,
                non_sequences=non_sequences_in)
        if self.backwards:
            seqs = tuple(seq[::-1] for seq in seqs)
        # returns: final_out, states, outputs
        return self.group_outputs(seqs)

    def make_inits(self, nontrainable_recurrent_inits, batch_size,
                   include_nones=False, do_eval=False):
        if nontrainable_recurrent_inits is None:
            nontrainable_recurrent_inits = []
        else:
            nontrainable_recurrent_inits = list(nontrainable_recurrent_inits)
        # combine trainable and nontrainable inits
        inits_in = []
        for rec in self.recurrences:
            if rec.init is None:
                # nontrainable init is passed in as argument
                try:
                    inits_in.append(nontrainable_recurrent_inits.pop(0))
                except IndexError:
                    raise Exception('Too few nontrainable_recurrent_inits. '
                        ' Init for {} onwards missing'.format(rec))
            elif rec.init == OutputOnly:
                # no init needed
                if include_nones:
                    inits_in.append(None)
            else:
                # trainable inits must be expanded to batch size
                trainable = expand_to_batch(rec.init, batch_size)
                if do_eval:
                    trainable = trainable.eval()
                inits_in.append(trainable)
            # FIXME: make dropout masks here
        return inits_in

    def make_nonsequences(self, non_sequences,
                          include_params=True, do_eval=False):
        non_sequences_in = []
        if non_sequences is None:
            non_sequences = []
        for unit in self.units:
            # previous units already popped off
            unit_nonseq = list(non_sequences)
            for ns in unit.non_sequences:
                if ns.func is not None:
                    if do_eval:
                        func = self.ns_func(ns)
                        val = func(unit_nonseq[ns.idx])
                    else:
                        val = ns.func(unit_nonseq[ns.idx])
                    non_sequences_in.append(val)
                else:
                    non_sequences_in.append(non_sequences.pop(0))
            # FIXME: add dropout masks to nonseqs. Interleaved?
        if include_params:
            non_sequences_in.extend(self.unit_parameters_list())
        return non_sequences_in

    def group_outputs(self, seqs):
        """group outputs in a useful way"""
        # main output of final unit
        final_out = seqs[self.final_out_idx]
        # true recurrent states (inputs for next iteration)
        states = []
        # OutputOnly are not fed into next iteration
        outputs = []
        for (rec, seq) in zip(self.recurrences, seqs):
            if rec.init == OutputOnly:
                outputs.append(seq)
            else:
                states.append(seq)
        return final_out, states, outputs

    def ns_func(self, ns):
        if ns not in self._ns_func_cache:
            self._ns_func_cache[ns] = function(
                [ns.variable],
                ns.func(ns.variable))
        return self._ns_func_cache[ns]

    def unit_parameters_list(self):
        non_sequences_in = []
        for unit in self.units:
            non_sequences_in.extend(unit.parameters_list())
        return non_sequences_in

    def step(self, inputs, inputs_mask, *args):
        args_tail = list(args)
        grouped_rec = []
        grouped_nonseq = []
        recurrents_in = []
        recurrents_out = []
        out = inputs
        # group recurrents and non-sequences by unit
        # FIXME: separate leading extra sequences
        for unit in self.units:
            unit_rec = []
            for rec in unit.recurrences:
                if rec.init == OutputOnly:
                    # output only: scan has removed the None
                    recurrents_in.append(OutputOnly)
                else:
                    rec_in = args_tail.pop(0)
                    unit_rec.append(rec_in)
                    recurrents_in.append(rec_in)
            grouped_rec.append(unit_rec)
        for unit in self.units:
            if len(args_tail) < unit.n_nonseq:
                raise Exception('Not enough nonsequences')
            unit_nonseq, args_tail = \
                args_tail[:unit.n_nonseq], args_tail[unit.n_nonseq:]
            grouped_nonseq.append(unit_nonseq)
        # apply the units
        for (unit, unit_rec, unit_nonseq)  in zip(
                self.units, grouped_rec, grouped_nonseq):
            unit_recs_out = unit.step(out, unit_rec, unit_nonseq)
            # first recurrent output becomes new input
            out = unit_recs_out[0]
            recurrents_out.extend(unit_recs_out)
        # apply inputs mask to all recurrents
        inputs_mask_bcast = inputs_mask.dimshuffle(0, 'x')
        recurrents_out = [
            T.switch(inputs_mask_bcast, rec_out, rec_in)
            if rec_in is not OutputOnly else rec_out
            for (rec_out, rec_in) in zip(recurrents_out, recurrents_in)]
        # you probably only care about recurrents_out[this.final_out_idx]
        return recurrents_out

    def step_fun(self):
        if self._step_fun is None:
            all_inputs = [T.matrix('inputs'), T.vector('inputs_mask')]
            all_inputs.extend((
                rec.variable for rec in self.recurrences
                if not rec.init == OutputOnly))
            all_inputs.extend((
                nonseq.variable for nonseq in self.non_sequences))
            self._step_fun = function(
                all_inputs,
                self.step(*all_inputs),
                name='{}_step_fun'.format(self.name))
        return self._step_fun

    @property
    def final_out_idx(self):
        """Index (into ungrouped output of scan) of the final output.
        NB: DON'T use this to index into grouped output."""
        return -self.units[-1].n_rec

    @property
    def recurrences(self):
        return [rec for unit in self.units for rec in unit.recurrences]

    @property
    def non_sequences(self):
        return [nonseq for unit in self.units for nonseq in unit.non_sequences]


class LSTMUnit(Unit):
    def __init__(self, name, *args,
                 gate=None, dropout=0, trainable_initial=False, **kwargs):
        super().__init__(name)
        gate = gate if gate is not None else LSTM('gate', *args, **kwargs)
        self.add(gate)
        if trainable_initial:
            self.param('h_0', (self.gate.state_dims,),
                       init_f=init.Gaussian(fan_in=self.gate.state_dims))
            self.param('c_0', (self.gate.state_dims,),
                       init_f=init.Gaussian(fan_in=self.gate.state_dims))
            h_0 = self._h_0
            c_0 = self._c_0
        else:
            h_0 = None
            c_0 = None
        self.add_recurrence(T.matrix('h_tm1'), init=h_0, dropout=dropout)
        self.add_recurrence(T.matrix('c_tm1'), init=c_0, dropout=0)
        if self.gate.use_attention:
            # attention output
            self.add_recurrence(
                T.matrix('attention'), init=OutputOnly, dropout=0)

            self.add_non_sequence(T.tensor3('attended'))
            # precomputed from attended
            self.add_non_sequence(T.tensor3('attended_dot_u'),
                func=self.gate.attention_u, idx=0)
            self.add_non_sequence(T.matrix('attention_mask'))

    def step(self, out, unit_recs, unit_nonseqs):
        unit_recs = self.gate(out, *(unit_recs + unit_nonseqs))
        return unit_recs


class ResidualUnit(Unit):
    """Wraps another Unit"""
    def __init__(self, wrapped, var=None):
        super().__init__('residual_using_{}'.format(wrapped.name))
        self.wrapped = wrapped
        var = var if var is not None else T.matrix('residual')
        self.residual = Recurrence(var, OutputOnly, dropout=0)

    def step(self, out, unit_recs, unit_nonseqs):
        unit_recs = self.wrapped.step(out, unit_recs, unit_nonseqs)
        out += unit_recs[0]     # add residual
        return (out,) + unit_recs

    @property
    def recurrences(self):
        return (self.residual,) + self.wrapped.recurrences

    @property
    def non_sequences(self):
        return self.wrapped.non_sequences


class SeparatePathLSTMUnit(Unit):
    def __init__(self, name, input_dims, state_dims,
                 w=None, w_init=None, w_regularizer=None,
                 u=None, u_init=None, u_regularizer=None,
                 b=None, b_init=None, b_regularizer=None,
                 attention_dims=None, attended_dims=None,
                 layernorm=False,
                 dropout=0, trainable_initial=False):
        super().__init__(name)

        assert layernorm in (False, 'ba1', 'ba2')
        assert (attention_dims is None) == (attended_dims is None)

        if attended_dims is not None:
            input_dims += attended_dims

        self.input_dims = input_dims
        self.state_dims = state_dims
        self.layernorm = layernorm
        self.attention_dims = attention_dims
        self.attended_dims = attended_dims
        self.use_attention = attention_dims is not None

        if w_init is None: w_init = init.Gaussian(fan_in=input_dims)

        if u_init is None: u_init = init.Concatenated(
            [init.Orthogonal()]*5, axis=1)

        if b_init is None: b_init = init.Concatenated(
            [init.Constant(x) for x in [0.0, 1.0, 0.0, 0.0, 0.0]])

        self.param('w', (input_dims, state_dims*5), init_f=w_init, value=w)
        self.param('u', (state_dims, state_dims*5), init_f=u_init, value=u)
        self.param('b', (state_dims*5,), init_f=b_init, value=b)

        if self.use_attention:
            self.add(Linear('attention_u', attended_dims, attention_dims))
            self.param('attention_w', (state_dims, attention_dims),
                       init_f=init.Gaussian(fan_in=state_dims))
            self.param('attention_v', (attention_dims,),
                       init_f=init.Gaussian(fan_in=attention_dims))
            self.regularize(self._attention_w, w_regularizer)
            if layernorm == 'ba1':
                self.add(LayerNormalization('ln_a', (None, attention_dims)))

        self.regularize(self._w, w_regularizer)
        self.regularize(self._u, u_regularizer)
        self.regularize(self._b, b_regularizer)

        if layernorm == 'ba1':
            self.add(LayerNormalization('ln_1', (None, state_dims*5)))
            self.add(LayerNormalization('ln_2', (None, state_dims*5)))
        if layernorm:
            self.add(LayerNormalization('ln_h', (None, state_dims)))

        if trainable_initial:
            self.param('h_0', (self.state_dims,),
                       init_f=init.Gaussian(fan_in=self.state_dims))
            self.param('c_0', (self.state_dims,),
                       init_f=init.Gaussian(fan_in=self.state_dims))
            h_0 = self._h_0
            c_0 = self._c_0
        else:
            h_0 = None
            c_0 = None
        self.add_recurrence(T.matrix('h_tm1'), init=h_0, dropout=dropout)
        self.add_recurrence(T.matrix('c_tm1'), init=c_0, dropout=0)
        if self.use_attention:
            # attention output
            self.add_recurrence(
                T.matrix('attention'), init=OutputOnly, dropout=0)

            self.add_non_sequence(T.tensor3('attended'))
            # precomputed from attended
            self.add_non_sequence(T.tensor3('attended_dot_u'),
                func=self.attention_u, idx=0)
            self.add_non_sequence(T.matrix('attention_mask'))

        # separate path for connecting to character-level decoder
        self.add_recurrence(T.matrix('h_breve'),
                            init=OutputOnly)

    def step(self, inputs, unit_recs, unit_nonseqs):
        h_tm1, c_tm1 = unit_recs
        if self.use_attention:
            attended, attended_dot_u, attention_mask = unit_nonseqs
            # Non-precomputed part of the attention vector for this time step
            #   _ x batch_size x attention_dims
            h_dot_w = T.dot(h_tm1, self._attention_w)
            if self.layernorm == 'ba1': h_dot_w = self.ln_a(h_dot_w)
            h_dot_w = h_dot_w.dimshuffle('x',0,1)
            # Attention vector, with distributions over the positions in
            # attended. Elements that fall outside the sentence in each batch
            # are set to zero.
            #   sequence_length x batch_size
            # Note that attention.T is returned
            attention = softmax_masked(
                    T.dot(
                        T.tanh(attended_dot_u + h_dot_w),
                        self._attention_v).T,
                    attention_mask.T).T
            # Compressed attended vector, weighted by the attention vector
            #   batch_size x attended_dims
            compressed = (attended * attention.dimshuffle(0,1,'x')).sum(axis=0)
            # Append the compressed vector to the inputs and continue as usual
            inputs = T.concatenate([inputs, compressed], axis=1)
        if self.layernorm == 'ba1':
            x = (self.ln_1(T.dot(inputs, self._w)) +
                 self.ln_2(T.dot(h_tm1, self._u)))
        else:
            x = T.dot(inputs, self._w) + T.dot(h_tm1, self._u)
        x = x + self._b.dimshuffle('x', 0)
        def x_part(i): return x[:, i*self.state_dims:(i+1)*self.state_dims]
        i = T.nnet.sigmoid(x_part(0))
        f = T.nnet.sigmoid(x_part(1))
        o = T.nnet.sigmoid(x_part(2))
        c = T.tanh(        x_part(3))
        h_breve = T.tanh(  x_part(4))
        c_t = f*c_tm1 + i*c
        h_t = o*T.tanh(self.ln_h(c_t) if self.layernorm else c_t)
        if self.use_attention:
            return h_t, c_t, attention.T, h_breve
        else:
            return h_t, c_t, h_breve
