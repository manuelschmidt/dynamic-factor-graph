#!/usr/bin/env python

# encoding: utf-8
# File Name: dfg.py
# Author: Jiezhong Qiu
# Create Time: 2015/01/26 00:25
# TODO:

from collections import OrderedDict
import numpy as np
import theano
import theano.tensor as T
from sklearn.base import BaseEstimator
import logging
import time
import json
import datetime
import os
import cPickle as pickle
import factor_minibatch
import unittest
import matplotlib.pylab as plt


logger = logging.getLogger(__name__)
theano.config.exception_verbosity='high'
#mode = theano.Mode(linker='cvm')
#mode = 'DebugMode'
mode = 'FAST_COMPILE'
#mode = theano.Mode(optimizer=None)
#mode = 'ProfileMode'
DEBUG = True

class DFG(object):
    """     Dynamic factor graph class

    Support output types:
    real: linear output units, use mean-squared error
    binary: binary output units, use cross-entropy error
    softmax: single softmax out, use cross-entropy error
    """
    def __init__(self, n_hidden, n_obsv, n_step, order, n_seq, start, n_iter,
                factor_type='FIR'):
        self.n_hidden = n_hidden
        self.n_obsv = n_obsv
        self.n_step = n_step
        self.order = order
        self.n_seq = n_seq
        self.factor_type = factor_type
        self.start = start
        self.n_iter = n_iter
        # For mini-batch
        self.index = T.iscalar('index') # index to a [mini]batch
        self.n_ex  = T.iscalar('n_ex') # the number of examples
        self.batch_size = T.iscalar('batch_size')
        self.batch_start = self.index * self.batch_size
        self.batch_stop = T.minimum(self.n_ex, (self.index + 1) * self.batch_size)
        self.effective_batch_size = self.batch_stop - self.batch_start
        if self.factor_type == 'FIR':
            self.factor = factor_minibatch.FIR(n_hidden=self.n_hidden,
                                        n_obsv=self.n_obsv, n_step=self.n_step,
                                        order=self.order, n_seq=self.n_seq, start=self.start, n_iter=self.n_iter,
                                        batch_start=self.batch_start, batch_stop=self.batch_stop)
        else:
            raise NotImplementedError

        self.params_Estep = self.factor.params_Estep
        self.params_Mstep = self.factor.params_Mstep
        self.L1 = self.factor.L1
        self.L2_sqr = self.factor.L2_sqr

        self.y_pred = self.factor.y_pred
        self.z_pred = self.factor.z_pred
        self.y_next = self.factor.y_next
        self.z_next = self.factor.z_next
        self.z = self.factor.z

        self.updates = OrderedDict()
        for param in self.params_Estep:
            init = np.zeros(param.get_value(borrow=True).shape,
                    dtype=theano.config.floatX)

            self.updates[param] = theano.shared(init)
        for param in self.params_Mstep:
            init = np.zeros(param.get_value(borrow=True).shape,
                    dtype=theano.config.floatX)
            self.updates[param] = theano.shared(init)

        # Loss = ||Z*(t)-Z(t)||^2 + ||Y*(t) - Y(t)||^2
        self.z_std = self.z[self.start + self.order: self.start + self.order + self.n_iter]
        self.loss_Estep = lambda y : (self.se(self.y_pred, y) + self.se(self.z_pred, self.z[self.order:])) / n_seq
        self.loss_Mstep = lambda y : (self.se(self.y_next, y) + self.se(self.z_next, self.z_std)) / self.effective_batch_size
        self.test_loss = lambda y : self.se(self.y_next, y) / self.effective_batch_size

        # Smooth Term ||Z(t+1)-Z(t)||^2
        # Estep
        diag_Estep = np.zeros(((n_step + order)*n_hidden, (n_step+order)*n_hidden),
                                dtype=theano.config.floatX)
        np.fill_diagonal(diag_Estep[n_hidden:,:], 1.)
        np.fill_diagonal(diag_Estep[-n_hidden:,-n_hidden:], 1.)
        # (n_step+order) x n_seq x n_hdden
        z_flatten = T.flatten(self.z.dimshuffle(1, 0, 2), outdim=2)
        z_tm1 = T.dot(z_flatten, diag_Estep)
        self.smooth_Estep = self.se(z_flatten, z_tm1) / n_seq

        diag_Mstep = T.eye(self.n_iter*n_hidden, self.n_iter*n_hidden, n_hidden)
        for i in xrange(n_hidden):
            diag_Mstep = T.set_subtensor(diag_Mstep[-i-1, -i-1], 1)
        z_next_flatten = T.flatten(self.z_next.dimshuffle(1, 0, 2), outdim=2)
        z_next_tm1 = T.dot(z_next_flatten, diag_Mstep)
        self.smooth_Mstep = self.se(z_next_flatten, z_next_tm1) / self.effective_batch_size
    def se(self, y_1, y_2):
        return T.sum((y_1 - y_2) ** 2)
    def mse(self, y_1, y_2):
        # error between output and target
        return T.mean((y_1 - y_2) ** 2)
    def nmse(self, y_1, y_2):
        # treat y_1 as the approximation to y_2
        return self.mse(y_1, y_2) / self.mse(y_2, 0)


class MetaDFG(BaseEstimator):
    def __init__(self, n_hidden, n_obsv, n_step, order, n_seq, learning_rate_Estep=0.1, learning_rate_Mstep=0.1,
                n_epochs=100, batch_size=100, L1_reg=0.00, L2_reg=0.00, smooth_reg=0.00,
                learning_rate_decay=1, learning_rate_decay_every=100,
                factor_type='FIR', activation='tanh', final_momentum=0.9,
                initial_momentum=0.5, momentum_switchover=5,
                n_iter_low=[20,], n_iter_high=[50,], n_iter_change_every=50,
                snapshot_every=None, snapshot_path='tmp/'):
        self.n_hidden = int(n_hidden)
        self.n_obsv = int(n_obsv)
        self.n_step = int(n_step)
        self.order = int(order)
        self.n_seq = int(n_seq)
        self.learning_rate_Estep = float(learning_rate_Estep)
        self.learning_rate_Mstep = float(learning_rate_Mstep)
        self.learning_rate_decay = float(learning_rate_decay)
        self.learning_rate_decay_every=int(learning_rate_decay_every)
        self.n_epochs = int(n_epochs)
        self.batch_size = int(batch_size)
        self.L1_reg = float(L1_reg)
        self.L2_reg = float(L2_reg)
        self.smooth_reg = float(smooth_reg)
        self.factor_type = factor_type
        self.activation = activation
        self.initial_momentum = float(initial_momentum)
        self.final_momentum = float(final_momentum)
        self.momentum_switchover = int(momentum_switchover)
        self.n_iter_low = n_iter_low
        self.n_iter_high = n_iter_high
        assert(len(self.n_iter_low) == len(self.n_iter_high))
        self.n_iter_change_every = int(n_iter_change_every)
        if snapshot_every is not None:
            self.snapshot_every = int(snapshot_every)
        else:
            self.snapshot_every = None
        self.snapshot_path = snapshot_path
        self.ready()

    def ready(self):
        # observation (where first dimension is time)
        self.y = T.tensor3(name='y', dtype=theano.config.floatX)

        # learning rate
        self.lr = T.scalar()
        # For mini-batch
        self.start = T.iscalar('start')
        self.n_iter = T.iscalar('n_iter')

        if self.activation == 'tanh':
            activation = T.tanh
        elif self.activation == 'sigmoid':
            activation = T.nnet.sigmoid
        elif self.activation == 'relu':
            activation = lambda x: x * (x > 0)
        else:
            raise NotImplementedError

        self.dfg = DFG(n_hidden=self.n_hidden,
                        n_obsv=self.n_obsv, n_step=self.n_step,
                        order=self.order, n_seq=self.n_seq, start=self.start,
                        n_iter=self.n_iter, factor_type=self.factor_type)

    def shared_dataset(self, data):
        """ Load the dataset into shared variables """

        shared_data = theano.shared(np.asarray(data,
                                            dtype=theano.config.floatX))
        return shared_data

    def __getstate__(self, jsonobj=False):
        params = self.get_params() # all the parameters in self.__init__
        weights_E = [p.get_value().tolist() if jsonobj else p.get_value() for p in self.dfg.params_Estep]
        weights_M = [p.get_value().tolist() if jsonobj else p.get_value() for p in self.dfg.params_Mstep]
        weights = (weights_E, weights_M)
        state = (params, weights)
        return state

    def _set_weights(self, weights):
        weights_E, weights_M = weights
        i = iter(weights_E)
        for param in self.dfg.params_Estep:
            param.set_value(i.next())
        i = iter(weights_M)
        for param in self.dfg.params_Mstep:
            param.set_value(i.next())

    def __setstate__(self, state):
        params, weights = state
        self.set_params(**params)
        self.ready()
        self._set_weights(weights)

    def save(self, fpath='.', fname=None):
        """Save a pickled representation of model state. """
        fpathstart, fpathext = os.path.splitext(fpath)
        if fpathext == '.pkl':
            fpath, fname = os.path.split(fpath)
        elif fpathext == '.json':
            fpath, fname = os.path.split(fpath)
        elif fname is None:
            # Generate filename based on date
            date_obj = datetime.datetime.now()
            date_str = date_obj.strftime('%Y-%m-%d-%H:%M:%S')
            class_name = self.__class__.__name__
            fname = '%s.%s.pkl' % (class_name, date_str)

        fabspath = os.path.join(fpath, fname)
        logger.info('Saving to %s ...' % fabspath)
        with open(fabspath, 'wb') as file:
            if fpathext == '.json':
                state = self.__getstate__(jsonobj=True)
                json.dump(state, file,
                            indent=4, separators=(',', ': '))
            else:
                state = self.__getstate__()
                pickle.dump(state, file, protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, fpath):
        """ Load model parameters from fpath. """
        logger.info('Loading from %s ...' % fpath)
        with open(fpath, 'rb') as file:
            state = pickle.load(file)
            self.__setstete__(state)

    def fit(self, Y_train, Y_test=None,
            validation_frequency=100):
        """Fit model

        Pass in Y_test to compute test error and report during training
            Y_train : ndarray (n_step, n_seq, n_out)
            Y_test  : ndarray (n_seq, n_seq, n_out)

        validation_frequency : int
            in terms of number of epoch
        """


        if Y_test is not None:
            self.interactive = True
            test_set_y = self.shared_dataset(Y_test)
        else:
            self.interactive = False
        train_set_y = self.shared_dataset(Y_train)
        n_train = train_set_y.get_value(borrow=True).shape[1]
        n_train_batches = int(np.ceil(float(n_train) / self.batch_size))
        if self.interactive:
            n_test = test_set_y.get_value(borrow=True).shape[1]
            n_test_batches = int(np.ceil(float(n_test) / self.batch_size))

        logger.info('...building the model')

        index = self.dfg.index
        n_ex = self.dfg.n_ex
        # learning rate (may change)
        l_r = T.scalar('l_r', dtype=theano.config.floatX)
        mom = T.scalar('mom', dtype=theano.config.floatX)

        cost_Estep = self.dfg.loss_Estep(self.y) \
                + self.dfg.smooth_Estep \
                + self.L1_reg * self.dfg.L1 \
                + self.L2_reg * self.dfg.L2_sqr

        cost_Mstep = self.dfg.loss_Mstep(self.y) \
                + self.dfg.smooth_Mstep \
                + self.L1_reg * self.dfg.L1 \
                + self.L2_reg * self.dfg.L2_sqr

        # mini-batch implement
        batch_size = self.dfg.batch_size
        batch_start = self.dfg.batch_start
        batch_stop = self.dfg.batch_stop
        effective_batch_size = self.dfg.effective_batch_size
        get_batch_size = theano.function(inputs=[index, n_ex, batch_size],
                                            outputs=effective_batch_size)

        compute_train_error_Estep = theano.function(inputs=[],
                                                outputs=self.dfg.loss_Estep(self.y),
                                                givens=OrderedDict([(self.y, train_set_y)]),
                                                mode=mode)

        compute_train_error_Mstep = theano.function(inputs=[index, n_ex, self.start, self.n_iter, batch_size],
                                        outputs=self.dfg.loss_Mstep(self.y),
                                        givens=OrderedDict([(self.y, train_set_y[self.start:self.start+self.n_iter, batch_start:batch_stop])]),
                                        mode=mode)
        if self.interactive:
            compute_test_error = theano.function(inputs=[index, n_ex, self.start, self.n_iter, batch_size],
                                                    outputs=[self.dfg.test_loss(self.y), self.dfg.y_next],
                                                    givens=OrderedDict([(self.y, test_set_y[:, batch_start:batch_stop])]),
                                                    mode=mode)


        # compute the gradient of cost with respect to theta = (W, W_in, W_out)
        # gradients on the weights using BPTT
        # E step
        gparams_Estep = []
        for param in self.dfg.params_Estep:
            gparam = T.grad(cost_Estep, param)
            gparams_Estep.append(gparam)

        updates_Estep = OrderedDict()
        for param, gparam in zip(self.dfg.params_Estep, gparams_Estep):
            weight_update = self.dfg.updates[param]
            upd = mom * weight_update - l_r * gparam
            updates_Estep[weight_update] = upd
            updates_Estep[param] = param + upd

        # M step
        gparams_Mstep = []
        for param in self.dfg.params_Mstep:
            gparam = T.grad(cost_Mstep, param)
            gparams_Mstep.append(gparam)

        updates_Mstep = OrderedDict()
        for param, gparam in zip(self.dfg.params_Mstep, gparams_Mstep):
            weight_update = self.dfg.updates[param]
            upd = mom * weight_update - l_r * gparam
            updates_Mstep[weight_update] = upd
            updates_Mstep[param] = param + upd

        # compiling a Theano function `train_model_Estep` that returns the
        # cost, but in the same time updates the parameter of the
        # model based on the rules defined in `updates_Estep`
        train_model_Estep = theano.function(inputs=[l_r, mom],
                                        outputs=[cost_Estep, self.dfg.loss_Estep(self.y), self.dfg.y_pred, self.dfg.z_pred],
                                        updates=updates_Estep,
                                        givens=OrderedDict([(self.y, train_set_y)]),
                                        mode=mode)
        # updates the parameter of the model based on
        # the rules defined in `updates_Mstep`
        train_model_Mstep = theano.function(inputs=[index, n_ex, l_r, mom, self.start, self.n_iter, batch_size],
                                        outputs=[cost_Mstep, self.dfg.y_next, self.dfg.z_next] + gparams_Mstep,
                                        updates=updates_Mstep,
                                        givens=OrderedDict([(self.y, train_set_y[self.start:self.start+self.n_iter, batch_start:batch_stop])]),
                                        mode=mode)
        ###############
        # TRAIN MODEL #
        ###############
        logger.info('... training')
        epoch = 0

        while (epoch < self.n_epochs):
            epoch = epoch + 1
            effective_momentum = self.final_momentum \
                        if epoch > self.momentum_switchover \
                        else self.initial_momentum
            example_cost, example_energy, example_y_pred, example_z_pred = train_model_Estep(self.learning_rate_Estep, 0.)
            logger.info('epoch %d E_step cost=%f energy=%f' % (epoch,
                                        example_cost, example_energy))
            for minibatch_idx in xrange(n_train_batches):
                average_cost = []
                for i in xrange(self.n_step):
                    n_iter = np.random.randint(low=self.n_iter_low[0],
                                                high=self.n_iter_high[0])
                    head = np.random.randint(self.n_step - n_iter)
                    example_cost, example_y_next, example_z_next, gW_o, gb_o, gW = train_model_Mstep(minibatch_idx, n_train, self.learning_rate_Mstep,
                                                effective_momentum, head, n_iter, self.batch_size)
                    average_cost.append(example_cost)
                logger.info('epoch %d batch %d M_step cost=%f' % (epoch, minibatch_idx, np.mean(average_cost)))
                iters = (epoch - 1) * n_train_batches + minibatch_idx + 1
                if iters % validation_frequency == 0:
                    # Computer loss on training set (conside Estep loss only)
                    train_loss_Estep = compute_train_error_Estep()
                    if self.interactive:
                        test_losses = [compute_test_error(i, n_test, self.n_step, Y_test.shape[0], self.batch_size)[0]
                                        for i in xrange(n_test_batches)]
                        test_batch_sizes = [get_batch_size(i, n_test, self.batch_size)
                                            for i in xrange(n_test_batches)]
                        this_test_loss = np.average(test_losses,
                                                    weights=test_batch_sizes)
                        logger.info('epoch %d, batch %d/%d, tr_loss %f, te_loss %f' % \
                                    (epoch, minibatch_idx + 1, n_train_batches, train_loss_Estep, this_test_loss))
                    else:
                        logger.info('epoch %d, batch %d/%d, tr_loss %f' % \
                                    (epoch, minibatch_idx + 1, n_train_batches, train_loss_Estep))
            # Update learning rate
            if self.learning_rate_decay_every is not None:
                if epoch % self.learning_rate_decay_every == 0:
                    self.learning_rate_Estep *= self.learning_rate_decay
                    self.learning_rate_Mstep *= self.learning_rate_decay
            if epoch % self.n_iter_change_every == 0:
                if len(self.n_iter_low) > 0:
                    self.n_iter_low = self.n_iter_low[1:]
                    self.n_iter_high = self.n_iter_high[1:]
            # Snapshot
            if self.snapshot_every is not None:
                if (epoch - 1) % self.snapshot_every == 0:
                    date_obj = datetime.datetime.now()
                    date_str = date_obj.strftime('%Y-%m-%d-%H:%M:%S')
                    class_name = self.__class__.__name__
                    fname = '%s.%s-snapshot-%d.png' % (class_name, date_str, epoch)
                    plt.figure()
                    n = Y_train.shape[0] + Y_test.shape[0]
                    x = np.linspace(0, n, n)
                    len_train = Y_train.shape[0]
                    x_train, x_test = x[:len_train], x[len_train:]
                    plt.plot(x_train, np.squeeze(Y_train), 'b', linewidth=2)
                    plt.plot(x_train, np.squeeze(example_y_pred), 'r', linewidth=2)
                    plt.savefig(self.snapshot_path + fname)
                    plt.close()
                    if self.interactive:
                        y_test_next = compute_test_error(0, n_test, self.n_step, Y_test.shape[0], self.batch_size)[1]
                        #logger.info('epoch %d test loss=%f' % (epoch, test_loss))
                        plt.figure()
                        plt.plot(x_test, np.squeeze(Y_test), 'b', linewidth=2)
                        plt.plot(x_test, np.squeeze(y_test_next), 'r', linewidth=2)
                        fname = '%s.%s-snapshot-%d_test.png' % (class_name, date_str, epoch)
                        plt.ylim(-3, 3)
                        plt.savefig(self.snapshot_path + fname)
                        plt.close()
            '''
            # Snapshot
            if self.snapshot_every is not None:
                if (epoch + 1) % self.snapshot_every == 0:
                    date_obj = datetime.datetime.now()
                    date_str = date_obj.strftime('%Y-%m-%d-%H:%M:%S')
                    class_name = self.__class__.__name__
                    fname = '%s.%s-snapshot-%d.pkl' % (class_name, date_str, epoch + 1)
                    fabspath = os.path.join(self.snapshot_path, fname)
                    self.save(fpath=fabspath)
            '''
class sinTestCase(unittest.TestCase):
    def runTest(self):
        n = 2500
        x = np.linspace(0, n, n)
        sita = [.2, .331, .42, .51, .74]
        sita = sita[:3]
        y = np.zeros(n)
        for item in sita:
            y += np.sin(item * x)
        # n_t x n_seq x n_in
        n_train = n - 500
        n_test = 500
        y_train = y[:n_train]
        y_test = y[n_train:]
        y_train = y_train.reshape(n_train, 1, 1)
        y_test = y_test.reshape(n_test, 1, 1)
        dfg = MetaDFG(n_hidden=3, n_obsv=1, n_step=n_train, order=25, n_seq=1, learning_rate_Estep=0.01, learning_rate_Mstep=0.001,
                n_epochs=1000, batch_size=1, snapshot_every=1, L1_reg=0.02, L2_reg=0.02, smooth_reg=0.01,
                learning_rate_decay=.9, learning_rate_decay_every=50,
                n_iter_low=[20, 20, 20, 20] , n_iter_high=[31, 51, 71, 101], n_iter_change_every=15,
                final_momentum=0.9,
                initial_momentum=0.5, momentum_switchover=500)
        dfg.fit(y_train, y_test, validation_frequency=1)
        assert True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
