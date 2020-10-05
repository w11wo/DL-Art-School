import torch
import torch.nn as nn
from models.networks import define_F
from models.loss import GANLoss
import random
import functools
import torchvision


def create_loss(opt_loss, env):
    type = opt_loss['type']
    if type == 'pix':
        return PixLoss(opt_loss, env)
    elif type == 'feature':
        return FeatureLoss(opt_loss, env)
    elif type == 'interpreted_feature':
        return InterpretedFeatureLoss(opt_loss, env)
    elif type == 'generator_gan':
        return GeneratorGanLoss(opt_loss, env)
    elif type == 'discriminator_gan':
        return DiscriminatorGanLoss(opt_loss, env)
    elif type == 'geometric':
        return GeometricSimilarityGeneratorLoss(opt_loss, env)
    elif type == 'translational':
        return TranslationInvarianceLoss(opt_loss, env)
    elif type == 'recursive':
        return RecursiveInvarianceLoss(opt_loss, env)
    else:
        raise NotImplementedError


# Converts params to a list of tensors extracted from state. Works with list/tuple params as well as scalars.
def extract_params_from_state(params, state, root=True):
    if isinstance(params, list) or isinstance(params, tuple):
        p = [extract_params_from_state(r, state, False) for r in params]
    elif isinstance(params, str):
        p = state[params]
    else:
        p = params
    # The root return must always be a list.
    if root and not isinstance(p, list):
        p = [p]
    return p


class ConfigurableLoss(nn.Module):
    def __init__(self, opt, env):
        super(ConfigurableLoss, self).__init__()
        self.opt = opt
        self.env = env
        self.metrics = []

    # net is either a scalar network being trained or a list of networks being trained, depending on the configuration.
    def forward(self, net, state):
        raise NotImplementedError

    def extra_metrics(self):
        return self.metrics


def get_basic_criterion_for_name(name, device):
    if name == 'l1':
        return nn.L1Loss().to(device)
    elif name == 'l2':
        return nn.MSELoss().to(device)
    elif name == 'cosine':
        return nn.CosineEmbeddingLoss().to(device)
    else:
        raise NotImplementedError


class PixLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(PixLoss, self).__init__(opt, env)
        self.opt = opt
        self.criterion = get_basic_criterion_for_name(opt['criterion'], env['device'])

    def forward(self, _, state):
        return self.criterion(state[self.opt['fake']], state[self.opt['real']])


class FeatureLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(FeatureLoss, self).__init__(opt, env)
        self.opt = opt
        self.criterion = get_basic_criterion_for_name(opt['criterion'], env['device'])
        self.netF = define_F(which_model=opt['which_model_F'],
                             load_path=opt['load_path'] if 'load_path' in opt.keys() else None).to(self.env['device'])
        if not env['opt']['dist']:
            self.netF = torch.nn.parallel.DataParallel(self.netF)

    def forward(self, _, state):
        with torch.no_grad():
            logits_real = self.netF(state[self.opt['real']])
        logits_fake = self.netF(state[self.opt['fake']])
        if self.opt['criterion'] == 'cosine':
            return self.criterion(logits_fake, logits_real, torch.ones(1, device=logits_fake.device))
        else:
            return self.criterion(logits_fake, logits_real)


# Special form of feature loss which first computes the feature embedding for the truth space, then uses a second
# network which was trained to replicate that embedding on an altered input space (for example, LR or greyscale) to
# compute the embedding in the generated space. Useful for weakening the influence of the feature network in controlled
# ways.
class InterpretedFeatureLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(InterpretedFeatureLoss, self).__init__(opt, env)
        self.opt = opt
        self.criterion = get_basic_criterion_for_name(opt['criterion'], env['device'])
        self.netF_real = define_F(which_model=opt['which_model_F']).to(self.env['device'])
        self.netF_gen = define_F(which_model=opt['which_model_F'], load_path=opt['load_path']).to(self.env['device'])
        if not env['opt']['dist']:
            self.netF_real = torch.nn.parallel.DataParallel(self.netF_real)
            self.netF_gen = torch.nn.parallel.DataParallel(self.netF_gen)

    def forward(self, _, state):
        logits_real = self.netF_real(state[self.opt['real']])
        logits_fake = self.netF_gen(state[self.opt['fake']])
        return self.criterion(logits_fake, logits_real)


class GeneratorGanLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(GeneratorGanLoss, self).__init__(opt, env)
        self.opt = opt
        self.criterion = GANLoss(opt['gan_type'], 1.0, 0.0).to(env['device'])
        self.noise = None if 'noise' not in opt.keys() else opt['noise']
        self.detach_real = opt['detach_real'] if 'detach_real' in opt.keys() else True
        # This is a mechanism to prevent backpropagation for a GAN loss if it goes too low. This can be used to balance
        # generators and discriminators by essentially having them skip steps while their counterparts "catch up".
        self.min_loss = opt['min_loss'] if 'min_loss' in opt.keys() else 0
        self.loss_rotating_buffer = torch.zeros(10, requires_grad=False)
        self.rb_ptr = 0
        self.losses_computed = 0

    def forward(self, _, state):
        netD = self.env['discriminators'][self.opt['discriminator']]
        real = extract_params_from_state(self.opt['real'], state)
        fake = extract_params_from_state(self.opt['fake'], state)
        if self.noise:
            nreal = []
            nfake = []
            for i, t in enumerate(real):
                if isinstance(t, torch.Tensor):
                    nreal.append(t + torch.randn_like(t) * self.noise)
                    nfake.append(fake[i] + torch.randn_like(t) * self.noise)
                else:
                    nreal.append(t)
                    nfake.append(fake[i])
            real = nreal
            fake = nfake
        if self.opt['gan_type'] in ['gan', 'pixgan', 'pixgan_fea']:
            pred_g_fake = netD(*fake)
            loss = self.criterion(pred_g_fake, True)
        elif self.opt['gan_type'] == 'ragan':
            pred_d_real = netD(*real)
            if self.detach_real:
                pred_d_real = pred_d_real.detach()
            pred_g_fake = netD(*fake)
            loss = (self.criterion(pred_d_real - torch.mean(pred_g_fake), False) +
                    self.criterion(pred_g_fake - torch.mean(pred_d_real), True)) / 2
        else:
            raise NotImplementedError
        self.loss_rotating_buffer[self.rb_ptr] = loss.item()
        self.rb_ptr = (self.rb_ptr + 1) % self.loss_rotating_buffer.shape[0]
        if torch.mean(self.loss_rotating_buffer) < self.min_loss:
            return 0
        self.losses_computed += 1
        self.metrics.append(("loss_counter", self.losses_computed))
        return loss


class DiscriminatorGanLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(DiscriminatorGanLoss, self).__init__(opt, env)
        self.opt = opt
        self.criterion = GANLoss(opt['gan_type'], 1.0, 0.0).to(env['device'])
        self.noise = None if 'noise' not in opt.keys() else opt['noise']
        # This is a mechanism to prevent backpropagation for a GAN loss if it goes too low. This can be used to balance
        # generators and discriminators by essentially having them skip steps while their counterparts "catch up".
        self.min_loss = opt['min_loss'] if 'min_loss' in opt.keys() else 0
        self.loss_rotating_buffer = torch.zeros(10, requires_grad=False)
        self.rb_ptr = 0
        self.losses_computed = 0

    def forward(self, net, state):
        self.metrics = []
        real = extract_params_from_state(self.opt['real'], state)
        real = [r.detach() for r in real]
        fake = extract_params_from_state(self.opt['fake'], state)
        fake = [f.detach() for f in fake]
        if self.noise:
            nreal = []
            nfake = []
            for i, t in enumerate(real):
                if isinstance(t, torch.Tensor):
                    nreal.append(t + torch.randn_like(t) * self.noise)
                    nfake.append(fake[i] + torch.randn_like(t) * self.noise)
                else:
                    nreal.append(t)
                    nfake.append(fake[i])
            real = nreal
            fake = nfake
        d_real = net(*real)
        d_fake = net(*fake)

        if self.opt['gan_type'] in ['gan', 'pixgan']:
            self.metrics.append(("d_fake", torch.mean(d_fake)))
            self.metrics.append(("d_real", torch.mean(d_real)))
            l_real = self.criterion(d_real, True)
            l_fake = self.criterion(d_fake, False)
            l_total = l_real + l_fake
            loss = l_total
        elif self.opt['gan_type'] == 'ragan':
            d_fake_diff = d_fake - torch.mean(d_real)
            self.metrics.append(("d_fake_diff", torch.mean(d_fake_diff)))
            loss = (self.criterion(d_real - torch.mean(d_fake), True) +
                    self.criterion(d_fake_diff, False))
        else:
            raise NotImplementedError
        self.loss_rotating_buffer[self.rb_ptr] = loss.item()
        self.rb_ptr = (self.rb_ptr + 1) % self.loss_rotating_buffer.shape[0]
        if torch.mean(self.loss_rotating_buffer) < self.min_loss:
            return 0
        self.losses_computed += 1
        self.metrics.append(("loss_counter", self.losses_computed))
        return loss


# Computes a loss created by comparing the output of a generator to the output from the same generator when fed an
# input that has been altered randomly by rotation or flip.
# The "real" parameter to this loss is the actual output of the generator (from an injection point)
# The "fake" parameter is the LR input that produced the "real" parameter when fed through the generator.
class GeometricSimilarityGeneratorLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(GeometricSimilarityGeneratorLoss, self).__init__(opt, env)
        self.opt = opt
        self.generator = opt['generator']
        self.criterion = get_basic_criterion_for_name(opt['criterion'], env['device'])
        self.gen_input_for_alteration = opt['input_alteration_index'] if 'input_alteration_index' in opt.keys() else 0
        self.gen_output_to_use = opt['generator_output_index'] if 'generator_output_index' in opt.keys() else None
        self.detach_fake = opt['detach_fake'] if 'detach_fake' in opt.keys() else False

    # Returns a random alteration and its counterpart (that undoes the alteration)
    def random_alteration(self):
        return random.choice([(functools.partial(torch.flip, dims=(2,)), functools.partial(torch.flip, dims=(2,))),
                              (functools.partial(torch.flip, dims=(3,)), functools.partial(torch.flip, dims=(3,))),
                              (functools.partial(torch.rot90, k=1, dims=[2,3]), functools.partial(torch.rot90, k=3, dims=[2,3])),
                              (functools.partial(torch.rot90, k=2, dims=[2,3]), functools.partial(torch.rot90, k=2, dims=[2,3])),
                              (functools.partial(torch.rot90, k=3, dims=[2,3]), functools.partial(torch.rot90, k=1, dims=[2,3]))])

    def forward(self, net, state):
        self.metrics = []
        net = self.env['generators'][self.generator]  # Get the network from an explicit parameter.
                                                    # The <net> parameter is not reliable for generator losses since often they are combined with many networks.
        fake = extract_params_from_state(self.opt['fake'], state)
        alteration, undo_fn = self.random_alteration()
        altered = []
        for i, t in enumerate(fake):
            if i == self.gen_input_for_alteration:
                altered.append(alteration(t))
            else:
                altered.append(t)
        if self.detach_fake:
            with torch.no_grad():
                upsampled_altered = net(*altered)
        else:
            upsampled_altered = net(*altered)

        if self.gen_output_to_use is not None:
            upsampled_altered = upsampled_altered[self.gen_output_to_use]

        # Undo alteration on HR image
        upsampled_altered = undo_fn(upsampled_altered)

        if self.opt['criterion'] == 'cosine':
            return self.criterion(state[self.opt['real']], upsampled_altered, torch.ones(1, device=upsampled_altered.device))
        else:
            return self.criterion(state[self.opt['real']], upsampled_altered)


# Computes a loss created by comparing the output of a generator to the output from the same generator when fed an
# input that has been translated in a random direction.
# The "real" parameter to this loss is the actual output of the generator on the top left image patch.
# The "fake" parameter is the output base fed into a ImagePatchInjector.
class TranslationInvarianceLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(TranslationInvarianceLoss, self).__init__(opt, env)
        self.opt = opt
        self.generator = opt['generator']
        self.criterion = get_basic_criterion_for_name(opt['criterion'], env['device'])
        self.gen_input_for_alteration = opt['input_alteration_index'] if 'input_alteration_index' in opt.keys() else 0
        self.gen_output_to_use = opt['generator_output_index'] if 'generator_output_index' in opt.keys() else None
        self.patch_size = opt['patch_size']
        self.overlap = opt['overlap']  # For maximum overlap, can be calculated as 2*patch_size-image_size
        self.detach_fake = opt['detach_fake']
        assert(self.patch_size > self.overlap)

    def forward(self, net, state):
        self.metrics = []
        net = self.env['generators'][self.generator]  # Get the network from an explicit parameter.
        # The <net> parameter is not reliable for generator losses since often they are combined with many networks.

        border_sz = self.patch_size - self.overlap
        translation = random.choice([("top_right", border_sz, border_sz+self.overlap, 0, self.overlap),
                                 ("bottom_left", 0, self.overlap, border_sz, border_sz+self.overlap),
                                 ("bottom_right", 0, self.overlap, 0, self.overlap)])
        trans_name, hl, hh, wl, wh = translation
        # Change the "fake" input name that we are translating to one that specifies the random translation.
        fake = self.opt['fake'].copy()
        fake[self.gen_input_for_alteration] = "%s_%s" % (fake[self.gen_input_for_alteration], trans_name)
        input = extract_params_from_state(fake, state)
        if self.detach_fake:
            with torch.no_grad():
                trans_output = net(*input)
        else:
            trans_output = net(*input)
        if self.gen_output_to_use is not None:
            fake_shared_output = trans_output[self.gen_output_to_use][:, :, hl:hh, wl:wh]
        else:
            fake_shared_output = trans_output[:, :, hl:hh, wl:wh]

        # The "real" input is assumed to always come from the top left tile.
        gen_output = state[self.opt['real']]
        real_shared_output = gen_output[:, :, border_sz:border_sz+self.overlap, border_sz:border_sz+self.overlap]

        if self.opt['criterion'] == 'cosine':
            return self.criterion(fake_shared_output, real_shared_output, torch.ones(1, device=real_shared_output.device))
        else:
            return self.criterion(fake_shared_output, real_shared_output)


# Computes a loss repeatedly feeding the generator downsampled inputs created from its outputs. The expectation is
# that the generator's outputs do not change on repeated forward passes.
# The "real" parameter to this loss is the actual output of the generator.
# The "fake" parameter is the expected inputs that should be fed into the generator. 'input_alteration_index' is changed
#   so that it feeds the recursive input.
class RecursiveInvarianceLoss(ConfigurableLoss):
    def __init__(self, opt, env):
        super(RecursiveInvarianceLoss, self).__init__(opt, env)
        self.opt = opt
        self.generator = opt['generator']
        self.criterion = get_basic_criterion_for_name(opt['criterion'], env['device'])
        self.gen_input_for_alteration = opt['input_alteration_index'] if 'input_alteration_index' in opt.keys() else 0
        self.gen_output_to_use = opt['generator_output_index'] if 'generator_output_index' in opt.keys() else None
        self.recursive_depth = opt['recursive_depth']  # How many times to recursively feed the output of the generator back into itself
        self.downsample_factor = opt['downsample_factor']  # Just 1/opt['scale']. Necessary since this loss doesnt have access to opt['scale'].
        assert(self.recursive_depth > 0)

    def forward(self, net, state):
        self.metrics = []
        net = self.env['generators'][self.generator]  # Get the network from an explicit parameter.
        # The <net> parameter is not reliable for generator losses since they can be combined with many networks.

        gen_output = state[self.opt['real']]
        recurrent_gen_output = gen_output

        fake = self.opt['fake'].copy()
        input = extract_params_from_state(fake, state)
        for i in range(self.recursive_depth):
            input[self.gen_input_for_alteration] = torch.nn.functional.interpolate(recurrent_gen_output, scale_factor=self.downsample_factor, mode="nearest")
            recurrent_gen_output = net(*input)[self.gen_output_to_use]

        compare_real = gen_output
        compare_fake = recurrent_gen_output
        if self.opt['criterion'] == 'cosine':
            return self.criterion(compare_real, compare_fake, torch.ones(1, device=compare_real.device))
        else:
            return self.criterion(compare_real, compare_fake)

