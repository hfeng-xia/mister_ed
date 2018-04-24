""" Holds the various attacks we can do """
import torch
from torch.autograd import Variable, Function
from torch import optim


import utils.pytorch_utils as utils
import utils.image_utils as img_utils
import random
import sys
import custom_lpips.custom_dist_model as dm
import loss_functions as lf
import spatial_transformers as st
import torch.nn as nn
import adversarial_perturbations as ap
MAXFLOAT = 1e20


###############################################################################
#                                                                             #
#                      PARENT CLASS FOR ADVERSARIAL ATTACKS                   #
#                                                                             #
###############################################################################

class AdversarialAttack(object):
    """ Wrapper for adversarial attacks. Is helpful for when subsidiary methods
        are needed.
    """

    def __init__(self, classifier_net, normalizer, threat_model, use_gpu=False):
        """ Initializes things to hold to perform a single batch of
            adversarial attacks
        ARGS:
            classifier_net : nn.Module subclass - neural net that is the
                             classifier we're attacking
            normalizer : DifferentiableNormalize object - object to convert
                         input data to mean-zero, unit-var examples
            threat_model : ThreatModel object - object that allows us to create
                           per-minibatch adversarial examples
        """
        self.classifier_net = classifier_net
        self.normalizer = normalizer or utils.IdentityNormalize()
        self.use_gpu = use_gpu
        self.validator = lambda *args: None
        self.threat_model = threat_model

    @property
    def _dtype(self):
        return torch.cuda.FloatTensor if self.use_gpu else torch.FloatTensor

    def setup(self):
        self.classifier_net.eval()
        self.normalizer.differentiable_call()


    def eval(self, ground_examples, adversarials, labels, topk=1):
        """ Evaluates how good the adversarial examples are
        ARGS:
            ground_truths: Variable (NxCxHxW) - examples before we did
                           adversarial perturbation. Vals in [0, 1] range
            adversarials: Variable (NxCxHxW) - examples after we did
                           adversarial perturbation. Should be same shape and
                           in same order as ground_truth
            labels: Variable (longTensor N) - correct labels of classification
                    output
        RETURNS:
            tuple of (% of correctly classified original examples,
                      % of correctly classified adversarial examples)
        """
        ground_examples = utils.safe_var(ground_examples)
        adversarials = utils.safe_var(adversarials)
        labels = utils.safe_var(labels)

        normed_ground = self.normalizer.forward(ground_examples)
        ground_output = self.classifier_net.forward(normed_ground)

        normed_advs = self.normalizer.forward(adversarials)
        adv_output = self.classifier_net.forward(normed_advs)

        start_prec = utils.accuracy(ground_output.data, labels.data,
                                    topk=(topk,))
        adv_prec = utils.accuracy(adv_output.data, labels.data,
                                  topk=(topk,))

        return start_prec[0][0], adv_prec[0][0]


    def eval_attack_only(self, adversarials, labels, topk=1):
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

        adversarials = utils.safe_var(adversarials)
        labels = utils.safe_var(labels)
        normed_advs = self.normalizer.forward(adversarials)

        adv_output = self.classifier_net.forward(normed_advs)
        return utils.accuracy_int(adv_output, labels, topk=topk)



    def print_eval_str(self, ground_examples, adversarials, labels, topk=1):
        """ Prints how good this adversarial attack is
            (explicitly prints out %CorrectlyClassified(ground_examples)
            vs %CorrectlyClassified(adversarials)

        ARGS:
            ground_truths: Variable (NxCxHxW) - examples before we did
                           adversarial perturbation. Vals in [0, 1] range
            adversarials: Variable (NxCxHxW) - examples after we did
                           adversarial perturbation. Should be same shape and
                           in same order as ground_truth
            labels: Variable (longTensor N) - correct labels of classification
                    output
        RETURNS:
            None, prints some stuff though
        """

        og, adv = self.eval(ground_examples, adversarials, labels, topk=topk)
        print "Went from %s correct to %s correct" % (og, adv)



    def validation_loop(self, examples, labels, iter_no=None):
        """ Prints out validation values interim for use in iterative techniques
        ARGS:
            new_examples: Variable (NxCxHxW) - [0.0, 1.0] images to be
                          classified and compared against labels
            labels: Variable (longTensor
            N) - correct labels for indices of
                             examples
            iter_no: String - an extra thing for prettier prints
        RETURNS:
            None
        """
        normed_input = self.normalizer.forward(examples)
        new_output = self.classifier_net.forward(normed_input)
        new_prec = utils.accuracy(new_output.data, labels.data, topk=(1,))
        print_str = ""
        if isinstance(iter_no, int):
            print_str += "(iteration %02d): " % iter_no
        elif isinstance(iter_no, basestring):
            print_str += "(%s): " % iter_no
        else:
            pass

        print_str += " %s correct" % new_prec[0][0]

        print print_str




##############################################################################
#                                                                            #
#                         Fast Gradient Sign Method (FGSM)                   #
#                                                                            #
##############################################################################

class FGSM(AdversarialAttack):
    def __init__(self, classifier_net, normalizer, threat_model, loss_fxn,
                 use_gpu=False):
        super(FGSM, self).__init__(classifier_net, normalizer, threat_model,
                                   use_gpu=use_gpu)
        self.loss_fxn = loss_fxn

    def attack(self, examples, labels, step_size=0.05, verbose=True):

        """ Builds FGSM examples for the given examples with l_inf bound
        ARGS:
            classifier: Pytorch NN
            examples: Nxcxwxh tensor for N examples. NOT NORMALIZED (i.e. all
                      vals are between 0.0 and 1.0 )
            labels: single-dimension tensor with labels of examples (in same
                    order)
            step_size: float - how much we nudge each parameter along the
                               signs of its gradient
            normalizer: DifferentiableNormalize object to prep objects into
                        classifier
            evaluate: boolean, if True will validation results
            loss_fxn:  RegularizedLoss object - partially applied loss fxn that
                         takes [0.0, 1.0] image Variables and labels and outputs
                         a scalar loss variable. Also has a zero_grad method
        RETURNS:
            AdversarialPerturbation object with correct parameters.
            Calling perturbation() gets Variable of output and
            calling perturbation().data gets tensor of output
        """
        self.classifier_net.eval() # ALWAYS EVAL FOR BUILDING ADV EXAMPLES

        perturbation = self.threat_model(examples)

        var_examples = Variable(examples, requires_grad=True)
        var_labels = Variable(labels, requires_grad=False)

        ######################################################################
        #   Build adversarial examples                                       #
        ######################################################################

        # Fix the 'reference' images for the loss function
        self.loss_fxn.setup_attack_batch(var_examples)

        # take gradients
        loss = self.loss_fxn.forward(perturbation(var_examples), var_labels)
        torch.autograd.backward(loss)


        # add adversarial noise to each parameter
        update_fxn = lambda grad_data: step_size * torch.sign(grad_data)
        perturbation.update_params(update_fxn)


        if verbose:
            self.validation_loop(perturbation(var_examples), var_labels,
                                 iter_no='Post FGSM')

        # output tensor with the data
        self.loss_fxn.cleanup_attack_batch()
        perturbation.attach_originals(examples)
        return perturbation


##############################################################################
#                                                                            #
#                           PGD/FGSM^k/BIM                                   #
#                                                                            #
##############################################################################
# This goes by a lot of different names in the literature
# The key idea here is that we take many small steps of FGSM
# I'll call it PGD though

class PGD(AdversarialAttack):

    def __init__(self, classifier_net, normalizer, threat_model, loss_fxn,
                 use_gpu=False):
        super(PGD, self).__init__(classifier_net, normalizer, threat_model,
                                  use_gpu=use_gpu)
        self.loss_fxn = loss_fxn

    def attack(self, examples, labels, step_size=1.0/255.0,
               num_iterations=20, random_init=False, signed=True,
               verbose=True):
        """ Builds PGD examples for the given examples with l_inf bound and
            given step size. Is almost identical to the BIM attack, except
            we take steps that are proportional to gradient value instead of
            just their sign
        ARGS:
            examples: NxCxHxW tensor - for N examples, is NOT NORMALIZED
                      (i.e., all values are in between 0.0 and 1.0)
            labels: N longTensor - single dimension tensor with labels of
                    examples (in same order as examples)
            l_inf_bound : float - how much we're allowed to perturb each pixel
                          (relative to the 0.0, 1.0 range)
            step_size : float - how much of a step we take each iteration
            num_iterations: int - how many iterations we take
            random_init : bool - if True, we randomly pick a point in the
                               l-inf epsilon ball around each example
            signed : bool - if True, each step is
                            adversarial = adversarial + sign(grad)
                            [this is the form that madry et al use]
                            if False, each step is
                            adversarial = adversarial + grad
        RETURNS:
            NxCxHxW tensor with adversarial examples
        """

        ######################################################################
        #   Setups and assertions                                            #
        ######################################################################

        self.classifier_net.eval()

        if not verbose:
            self.validator = lambda ex, label, iter_no: None
        else:
            self.validator = self.validation_loop

        perturbation = self.threat_model(examples)


        var_examples = Variable(examples, requires_grad=True)
        var_labels = Variable(labels, requires_grad=False)

        ######################################################################
        #   Loop through iterations                                          #
        ######################################################################

        self.loss_fxn.setup_attack_batch(var_examples)
        self.validator(var_examples, var_labels, iter_no="START")

        # random initialization if necessary
        if random_init:
            perturbation.random_init()
            self.validator(perturbation(var_examples), var_labels,
                           iter_no="RANDOM")

        # Build optimizer techniques for both signed and unsigned methods
        optimizer = optim.Adam(perturbation.parameters(), lr=0.0005)
        update_fxn = lambda grad_data: step_size * torch.sign(grad_data)


        for iter_no in xrange(num_iterations):
            perturbation.zero_grad()
            loss = self.loss_fxn.forward(perturbation(var_examples), var_labels)
            torch.autograd.backward(loss)

            if signed:
                perturbation.update_params(update_fxn)
            else:
                optimizer.step()

            self.validator(perturbation(var_examples), var_labels,
                           iter_no=iter_no)

        perturbation.zero_grad()
        self.loss_fxn.cleanup_attack_batch()
        perturbation.attach_originals(examples)
        return perturbation


