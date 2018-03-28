""" Contains training code for adversarial training """

import torch
import torchvision
import torch.cuda as cuda
import torch.optim as optim
import torchvision.transforms as transforms

from torch.autograd import Variable

import random

import utils.pytorch_utils as utils
import utils.image_utils as img_utils
import adversarial_attacks as attacks
import checkpoints


##############################################################################
#                                                                            #
#                               ATTACK PARAMETERS OBJECT                     #
#                                                                            #
##############################################################################

class AdversarialAttackParameters(object):
    """ Wrapper to store an adversarial attack object as well as some extra
        parameters for how to use it in training
    """
    def __init__(self, adv_attack_obj, proportion_attacked,
                 attack_specific_params=None):
        """ Stores params for how to use adversarial attacks in training
        ARGS:
            adv_attack_obj : AdversarialAt tack subclass -
                             thing that actually does the attack
            proportion_attacked: float between [0.0, 1.0] - what proportion of
                                 the minibatch we build adv examples for
            attack_specific_params: possibly None dict, but possibly dict with
                                    specific parameters for attacks

        """
        self.adv_attack_obj = adv_attack_obj
        self.proportion_attacked = proportion_attacked

        attack_specific_params = attack_specific_params or {}
        self.attack_specific_params = attack_specific_params
        self.attack_kwargs = attack_specific_params.get('attack_kwargs', {})


        # Block to build fxn to select output based on which type of class
        if isinstance(adv_attack_obj, (attacks.FGSM, attacks.BIM,
                                       attacks.LInfPGD)):
            self.output_filter = lambda d: d # also selects output

        elif isinstance(adv_attack_obj, attacks.CWL2):
            def output_filter(output_dict, params=self.attack_specific_params):
                cutoff = (params or {}).get('cutoff', None)
                return attacks.CWL2.filter_outputs(output_dict,
                                                   cutoff_metric=cutoff)
            self.output_filter = output_filter

        else:
            raise Exception("Invalid attack type")

    def set_gpu(self, use_gpu):
        """ Propagates changes of the 'use_gpu' parameter down to the attack
        ARGS:
            use_gpu : bool - if True, the attack uses the GPU, ow it doesn't
        RETURNS:
            None
        """
        self.adv_attack_obj.use_gpu = use_gpu


    def attack(self, inputs, labels):
        """ Builds some adversarial examples given real inputs and labels
        ARGS:
            inputs : torch.Tensor (NxCxHxW) - tensor with examples needed
            labels : torch.Tensor (N) - tensor with the examples needed
        RETURNS:
            some sample of (self.proportion_attacked * N ) examples that are
            adversarial, and the corresponding NONADVERSARIAL LABELS

            output is a tuple with three tensors:
             (adv_examples, pre_adv_labels, selected_idxs )
             adv_examples: Tensor with shape (N'xCxHxW) [the perturbed outputs]
             pre_adv_labels: Tensor with shape (N') [original labels]
             selected_idxs : Tensor with shape (N') [idxs selected]
        """

        num_elements = inputs.shape[0]

        # SELECT int(self.proportion_attacked * batch_size)
        selected_idxs = sorted(random.sample(range(num_elements),
                                int(self.proportion_attacked * num_elements)))

        #selected_idxs = [i for i in xrange(num_elements)
        #                 if random.random() < self.proportion_attacked]
        selected_idxs = inputs.new(selected_idxs).long()
        if selected_idxs.numel() == 0:
            return (None, None, None)

        adv_inputs = inputs.index_select(0, selected_idxs)
        pre_adv_labels = labels.index_select(0, selected_idxs)

        adv_examples = self.adv_attack_obj.attack(adv_inputs, pre_adv_labels,
                                                  **self.attack_kwargs)
        adv_examples = self.output_filter(adv_examples)

        return (adv_examples, pre_adv_labels, selected_idxs)


    def eval(self, ground_inputs, adv_inputs, labels, idxs, topk=1):
        """ Outputs the accuracy of the adversarial examples

            NOTE: notice the difference between N and N' in the argument
        ARGS:
            ground_inputs: Variable (NxCxHxW) - examples before we did
                           adversarial perturbation. Vals in [0, 1] range
            adversarials: Variable (N'xCxHxW) - examples after we did
                           adversarial perturbation. Should be same shape and
                           in same order as ground_truth
            labels: Variable (longTensor N) - correct labels of classification
                    output
            idxs: Variable (longtensor N') - indices of ground_inputs/labels
                  used for adversarials.
        RETURNS:
            tuple of (% of correctly classified original examples,
                      % of correctly classified adversarial examples)

        """

        selected_grounds = ground_inputs.index_select(0, idxs)
        selected_labels = labels.index_select(0, idxs)
        return self.adv_attack_obj.eval(selected_grounds, adv_inputs,
                                        selected_labels, topk=topk)


    def eval_attack_only(self, adv_inputs, labels, topk=1):
        """ Outputs the accuracy of the adv_inputs only
        ARGS:
            adv_inputs: Variable NxCxHxW - examples after we did adversarial
                                           perturbation
            labels: Variable (longtensor N) - correct labels of classification
                                              output
            topk: int - criterion for 'correct' classification
        RETURNS:
            (int) number of correctly classified examples
        """

        return self.adv_attack_obj.eval_attack_only(adv_inputs, labels,
                                                    topk=topk)




##############################################################################
#                                                                            #
#                               TRAINING OBJECT                              #
#                                                                            #
##############################################################################



class AdversarialTraining(object):
    """ Wrapper for training of a NN with adversarial examples cooked in
    """

    def __init__(self, classifier_net, normalizer,
                 experiment_name, architecture_name):
        self.classifier_net =classifier_net
        self.normalizer = normalizer
        self.experiment_name = experiment_name
        self.architecture_name = architecture_name



        self.verbosity_level = None
        self.verbosity_minibatch = None
        self.verbosity_adv = None
        self.verbosity_epoch = None


    def set_verbosity(self, verbosity):
        """ Sets the verbosity level for training. Is called in .train method
            so this method doesn't need to be explicitly called.

            Verbosity is mapped from a string to a comparable int 'level'.
            verbosity_level : int - comparable value of verbosity
            verbosity_minibatch: int - we do a printout every this many
                                       minibatches
            verbosity_adv: int - we evaluate the efficacy of our attack every
                                 this many minibatches
            verbosity_epoch: int - we printout and checkpoint every this many
                                   epochs
        ARGS:
            verbosity : string ['low', 'medium', 'high', 'snoop'],
                        varying levels of verbosity in increasing order

        RETURNS: None
        """
        assert verbosity in ['low', 'medium', 'high', 'snoop']
        self.verbosity = verbosity
        self.verbosity_level = {'low': 0,
                                'medium': 1,
                                'high': 2,
                                'snoop': 420}[verbosity]

        self.verbosity_minibatch = {'medium': 2000,
                                    'high': 100,
                                    'snoop': 1}.get(verbosity)

        self.verbosity_adv = {'medium': 2000,
                              'high': 100,
                              'snoop': 1}.get(verbosity)

        self.verbosity_epoch = {'low': 100,
                                'medium': 10,
                                'high': 1,
                                'snoop': 1}.get(verbosity)


    def _attack_subroutine(self, attack_parameters, inputs, labels,
                           epoch_num, minibatch_num):
        """ Subroutine to run the specified attack on a minibatch and append
            the results to inputs/labels.

        NOTE: THIS DOES NOT MUTATE inputs/labels !!!!

        ARGS:
            attack_parameters:  AdversarialAttackParameters obj (or none) -
                                if not None, contains info on how to do adv
                                attacks. If None, we don't train adversarially
            inputs : Tensor (NxCxHxW) - minibatch of data we build adversarial
                                        examples for
            labels : Tensor (longtensor N) - minibatch of labels
            epoch_num : int - number of which epoch we're working on.
                        Is helpful for printing
            minibatch_num : int - number of which minibatch we're working on.
                            Is helpful for printing
        RETURNS:
            inputs, labels (but with augmentation in N b/c we built adversarial
                            examples)

        """
        if attack_parameters is None:
            return inputs, labels


        adv_data = attack_parameters.attack(inputs, labels)
        adv_inputs, adv_labels, adv_idxs = adv_data

        if (self.verbosity_level >= 1 and
            minibatch_num % self.verbosity_adv == self.verbosity_adv - 1):
            accuracy = attack_parameters.eval(inputs,
                                              adv_inputs,
                                              labels,
                                              adv_idxs)
            print('[%d, %5d] accuracy: (%.3f, %.3f)' %
              (epoch_num + 1, minibatch_num + 1, accuracy[1], accuracy[0]))

        inputs = torch.cat([inputs, adv_inputs], dim=0)
        labels = torch.cat([labels, adv_labels], dim=0)
        return inputs, labels


    def train(self, data_loader, num_epochs, loss_fxn,
              optimizer=None, attack_parameters=None, use_gpu=False,
              verbosity='medium'):
        """ Modifies the NN weights of self.classifier_net by training with
            the specified parameters s
        ARGS:
            data_loader: torch.utils.data.DataLoader - object that loads the
                         data
            num_epoch: int - number of epochs to train on
            loss_fxn: ????  - TBD
            optimizer: torch.Optimizer subclass - defaults to Adam with some
                       decent default params. Pass this in as an actual argument
                       to do anything different
            attack_parameters:  AdversarialAttackParameters obj (or none) -
                                if not None, contains info on how to do adv
                                attacks. If None, we don't train adversarially
            use_gpu : bool - if True, we use GPU's for things
            verbosity : string - must be 'low', 'medium', 'high', which
                        describes how much to print
        RETURNS:
            None, but modifies the classifier_net's weights
        """


        ######################################################################
        #   Setup/ input validations                                         #
        ######################################################################
        self.classifier_net.train() # in training mode
        assert isinstance(num_epochs, int)

        if attack_parameters is not None:
            assert isinstance(attack_parameters, AdversarialAttackParameters)
            # assert that the adv attacker uses the NN that's being trained
            assert (attack_parameters.adv_attack_obj.classifier_net ==
                    self.classifier_net)


        assert not (use_gpu and not cuda.is_available())
        if use_gpu:
            self.classifier_net.cuda()
        attack_parameters.set_gpu(use_gpu)

        # Verbosity parameters
        assert verbosity in ['low', 'medium', 'high', 'snoop', None]
        self.set_verbosity(verbosity)
        verbosity_level = self.verbosity_level
        verbosity_minibatch = self.verbosity_minibatch
        verbosity_epoch = self.verbosity_epoch


        # setup loss fxn, optimizer
        optimizer = optimizer or optim.Adam(self.classifier_net.parameters(),
                                            lr=0.001)

        ######################################################################
        #   Training loop                                                    #
        ######################################################################

        for epoch in range(num_epochs):
            running_loss = 0.0
            for i, data in enumerate(data_loader, 0):
                inputs, labels = data
                if use_gpu:
                    inputs = inputs.cuda()
                    labels = labels.cuda()


                # Build adversarial examples
                inputs, labels = self._attack_subroutine(attack_parameters,
                                                         inputs, labels,
                                                         epoch, i)

                # Now proceed with standard training
                self.normalizer.differentiable_call()
                self.classifier_net.train()
                inputs, labels = Variable(inputs), Variable(labels)
                optimizer.zero_grad()

                # forward step
                outputs = self.classifier_net.forward(self.normalizer(inputs))
                loss = loss_fxn.forward(outputs, labels)

                # backward step
                loss.backward()
                optimizer.step()

                # print things
                running_loss += loss.data[0]
                if (verbosity_level >= 1 and
                    i % verbosity_minibatch == verbosity_minibatch - 1):
                    print('[%d, %5d] loss: %.3f' %
                          (epoch + 1, i + 1, running_loss / 2000))
                    running_loss = 0.0

            # end_of_epoch
            if epoch > 0 and epoch % verbosity_epoch == 0:
                print "COMPLETED EPOCH %04d... checkpointing here" % epoch
                checkpoints.save_state_dict(self.experiment_name,
                                            self.architecture_name,
                                            epoch, self.classifier_net,
                                            k_highest=10)


        if verbosity_level >= 1:
            print 'Finished Training'

        return



############################################################################
#                                                                          #
#                                   EVALUATION OBJECT                      #
#                                                                          #
############################################################################

class AdversarialEvaluation(object):
    """ Wrapper for evaluation of NN's against adversarial examples
    """

    def __init__(self, classifier_net, normalizer):
        self.classifier_net = classifier_net
        self.normalizer = normalizer


    def evaluate(self, data_loader, attack_ensemble, use_gpu=False,
                 verbosity='medium', num_minibatches=None):
        """ Runs evaluation against attacks generated by attack ensemble over
            the entire training set
        ARGS:
            data_loader : torch.utils.data.DataLoader - object that loads the
                          evaluation data
            attack_ensemble : dict {string -> AdversarialAtttackParameters}
                             is a dict of attacks that we want to make.
                             None of the strings can be 'ground'

            use_gpu : bool - if True, we do things on the GPU
            num_minibatches: int - if not None, we only validate on a fixed
                                   number of minibatches
        RETURNS:
            a dict same keys as attack_ensemble, as well as the key 'ground'.
            The values are utils.AverageMeter objects
        """

        ######################################################################
        #   Setup input validations                                          #
        ######################################################################

        self.classifier_net.eval()
        assert isinstance(data_loader, torch.utils.data.DataLoader)

        assert 'ground' not in attack_ensemble
        validation_results = {k: utils.AverageMeter() for k in
                              attack_ensemble.keys() + ['ground']}

        utils.cuda_assert(use_gpu)
        if use_gpu:
            self.classifier_net.cuda()

        for attack_params in attack_ensemble.values():
            attack_params.set_gpu(use_gpu)


        ######################################################################
        #   Loop through validation set and attack efficacy                  #
        ######################################################################

        for i, data in enumerate(data_loader, 0):
            print "Starting minibatch %s..." % i

            if num_minibatches is not None and i >= num_minibatches:
                break

            inputs, labels = data
            if use_gpu:
                inputs = inputs.cuda()
                labels = labels.cuda()

            var_inputs = Variable(inputs, requires_grad=True)
            var_labels = Variable(labels, requires_grad=False)

            minibatch = float(len(inputs))

            # Do ground classification
            ground_output = self.classifier_net(self.normalizer(var_inputs))


            ground_accuracy_int = utils.accuracy_int(ground_output, var_labels,
                                                    topk=1)
            ground_avg = validation_results['ground']
            ground_avg.update(ground_accuracy_int / minibatch,
                              n=int(minibatch))


            # Loop through each attack in the ensemble
            for attack_name, attack_params in attack_ensemble.iteritems():
                print "\t (mb: %s) evaluating %s..." % (i, attack_name)
                attack_out_tuple = attack_params.attack(var_inputs.data,
                                                        var_labels.data)
                attack_examples = Variable(attack_out_tuple[0])
                pre_adv_labels = Variable(attack_out_tuple[1])

                attack_accuracy_int = attack_params.eval_attack_only(
                                                attack_examples,
                                                pre_adv_labels, topk=1)

                attack_avg = validation_results[attack_name]
                attack_avg.update(attack_accuracy_int / minibatch,
                                  n=int(minibatch))


        return {k: meter.avg for k, meter in validation_results.iteritems()}




