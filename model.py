import torch
import torch.nn as nn
from torch.nn import functional as F
import torch.nn.init
import torchvision.models as models
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import torch.backends.cudnn as cudnn
from torch.nn.utils.clip_grad import clip_grad_norm_
import numpy as np
from collections import OrderedDict
from random import randint

def l2norm(X):
    """L2-normalize columns of X
    """
    norm = torch.pow(X, 2).sum(dim=1, keepdim=True).sqrt()
    X = torch.div(X, norm)
    return X


def EncoderImage(data_name, img_dim, embed_size, finetune=False,
                 cnn_type='vgg19', use_abs=False, no_imgnorm=False):
    """A wrapper to image encoders. Chooses between an encoder that uses
    precomputed image features, `EncoderImagePrecomp`, or an encoder that
    computes image features on the fly `EncoderImageFull`.
    """
    if data_name.endswith('_precomp'):
        img_enc = EncoderImagePrecomp(
            img_dim, embed_size, use_abs, no_imgnorm)
    else:
        img_enc = EncoderImageFull(
            embed_size, finetune, cnn_type, use_abs, no_imgnorm)

    return img_enc


# tutorials/09 - Image Captioning
class EncoderImageFull(nn.Module):

    def __init__(self, embed_size, finetune=False, cnn_type='vgg19',
                 use_abs=False, no_imgnorm=False):
        """Load pretrained VGG19 and replace top fc layer."""
        super(EncoderImageFull, self).__init__()
        self.embed_size = embed_size
        self.no_imgnorm = no_imgnorm
        self.use_abs = use_abs

        # Load a pre-trained model
        self.cnn = self.get_cnn(cnn_type, True)

        # For efficient memory usage.
        for param in self.cnn.parameters():
            param.requires_grad = finetune

        # Replace the last fully connected layer of CNN with a new one
        if cnn_type.startswith('vgg'):
            self.fc = nn.Linear(self.cnn.classifier._modules['6'].in_features,
                                embed_size)
            self.cnn.classifier = nn.Sequential(
                *list(self.cnn.classifier.children())[:-1])
        elif cnn_type.startswith('resnet'):
            self.fc = nn.Linear(self.cnn.module.fc.in_features, embed_size)
            self.cnn.module.fc = nn.Sequential()

        self.init_weights()

    def get_cnn(self, arch, pretrained):
        """Load a pretrained CNN and parallelize over GPUs
        """
        if pretrained:
            print("=> using pre-trained model '{}'".format(arch))
            model = models.__dict__[arch](pretrained=True)
        else:
            print("=> creating model '{}'".format(arch))
            model = models.__dict__[arch]()

        if arch.startswith('alexnet') or arch.startswith('vgg'):
            model.features = nn.DataParallel(model.features)
            model.cuda()
        else:
            model = nn.DataParallel(model).cuda()

        return model

    def load_state_dict(self, state_dict):
        """
        Handle the models saved before commit pytorch/vision@989d52a
        """
        if 'cnn.classifier.1.weight' in state_dict:
            state_dict['cnn.classifier.0.weight'] = state_dict[
                'cnn.classifier.1.weight']
            del state_dict['cnn.classifier.1.weight']
            state_dict['cnn.classifier.0.bias'] = state_dict[
                'cnn.classifier.1.bias']
            del state_dict['cnn.classifier.1.bias']
            state_dict['cnn.classifier.3.weight'] = state_dict[
                'cnn.classifier.4.weight']
            del state_dict['cnn.classifier.4.weight']
            state_dict['cnn.classifier.3.bias'] = state_dict[
                'cnn.classifier.4.bias']
            del state_dict['cnn.classifier.4.bias']

        super(EncoderImageFull, self).load_state_dict(state_dict)

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.fc.in_features +
                                  self.fc.out_features)
        self.fc.weight.data.uniform_(-r, r)
        self.fc.bias.data.fill_(0)

    def forward(self, images):
        """Extract image feature vectors."""
        features = self.cnn(images)

        # normalization in the image embedding space
        features = l2norm(features)

        # linear projection to the joint embedding space
        features = self.fc(features)

        # normalization in the joint embedding space
        if not self.no_imgnorm:
            features = l2norm(features)

        # take the absolute value of the embedding (used in order embeddings)
        if self.use_abs:
            features = torch.abs(features)

        return features


class EncoderImagePrecomp(nn.Module):

    def __init__(self, img_dim, embed_size, use_abs=False, no_imgnorm=False):
        super(EncoderImagePrecomp, self).__init__()
        self.embed_size = embed_size
        self.no_imgnorm = no_imgnorm
        self.use_abs = use_abs

        self.fc = nn.Linear(img_dim, embed_size)

        self.init_weights()

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.fc.in_features +
                                  self.fc.out_features)
        self.fc.weight.data.uniform_(-r, r)
        self.fc.bias.data.fill_(0)

    def forward(self, images):
        """Extract image feature vectors."""
        # assuming that the precomputed features are already l2-normalized

        features = self.fc(images)

        # normalize in the joint embedding space
        if not self.no_imgnorm:
            features = l2norm(features)

        # take the absolute value of embedding (used in order embeddings)
        if self.use_abs:
            features = torch.abs(features)

        return features

    def load_state_dict(self, state_dict):
        """Copies parameters. overwritting the default one to
        accept state_dict from Full model
        """
        own_state = self.state_dict()
        new_state = OrderedDict()
        for name, param in state_dict.items():
            if name in own_state:
                new_state[name] = param

        super(EncoderImagePrecomp, self).load_state_dict(new_state)


# tutorials/08 - Language Model
# RNN Based Language Model
class EncoderText(nn.Module):

    def __init__(self, vocab_size, word_dim, embed_size, num_layers,
                 use_abs=False):
        super(EncoderText, self).__init__()
        self.use_abs = use_abs
        self.embed_size = embed_size

        # word embedding
        self.embed = nn.Embedding(vocab_size, word_dim)

        # caption embedding
        self.rnn = nn.GRU(word_dim, embed_size, num_layers, batch_first=True)

        self.init_weights()

    def init_weights(self):
        self.embed.weight.data.uniform_(-0.1, 0.1)

    def forward(self, x, lengths):
        """Handles variable size captions
        """
        # Embed word ids to vectors
        x = self.embed(x)
        packed = pack_padded_sequence(x, lengths, batch_first=True)

        # Forward propagate RNN
        out, _ = self.rnn(packed)

        # Reshape *final* output to (batch_size, hidden_size)
        padded = pad_packed_sequence(out, batch_first=True)
        I = torch.LongTensor(lengths).view(-1, 1, 1)
        I = Variable(I.expand(x.size(0), 1, self.embed_size)-1).cuda()
        out = torch.gather(padded[0], 1, I).squeeze(1)

        # normalization in the joint embedding space
        out = l2norm(out)

        # take absolute value, used by order embeddings
        if self.use_abs:
            out = torch.abs(out)

        return out


def cosine_sim(im, s):
    """Cosine similarity between all the image and sentence pairs
    """
    return im.mm(s.t())


def order_sim(im, s):
    """Order embeddings similarity measure $max(0, s-im)$
    """
    YmX = (s.unsqueeze(1).expand(s.size(0), im.size(0), s.size(1))
           - im.unsqueeze(0).expand(s.size(0), im.size(0), s.size(1)))
    score = -YmX.clamp(min=0).pow(2).sum(2).sqrt().t()
    return score


class ContrastiveLoss(nn.Module):
    """
    Compute contrastive loss
    """

    def __init__(self, opt):
        super(ContrastiveLoss, self).__init__()

        if opt.measure == 'order':
            self.sim = order_sim
        else:
            self.sim = cosine_sim

        self.opt = opt

        # "g" represents "global"
        self.g_alpha = self.opt.global_alpha
        self.g_beta= self.opt.global_beta # W_it
        self.g_ep_posi = self.opt.global_ep_posi # W_ii
        self.g_ep_nega = self.opt.global_ep_nega

        # "l" represents "local"
        self.l_alpha = self.opt.local_alpha
        self.l_ep = self.opt.local_ep

    def forward(self, im, s, mb_img, mb_cap, mb_ind, indices):

        if self.opt.max_violation or self.opt.sum_violation:

            diagonal = scores.diag().view(im.size()[0], 1)
            d1 = diagonal.expand_as(scores)
            d2 = diagonal.t().expand_as(scores)

            cost_s = (self.opt.margin + scores - d1).clamp(min=0)
            cost_im = (self.opt.margin + scores - d2).clamp(min=0)

            mask = torch.eye(im.size()[0]) > .5
            I = Variable(mask)
            if torch.cuda.is_available():
                I = I.cuda()
            cost_s = cost_s.masked_fill_(I, 0)
            cost_im = cost_im.masked_fill_(I, 0)

            if self.opt.max_violation:

                cost_s = cost_s.max(1)[0]
                cost_im = cost_im.max(0)[0]

            return cost_s.sum() + cost_im.sum()

        bsize = im.size()[0]

        scores = self.sim(im, s)

        tmp  = torch.eye(bsize).cuda()

        s_diag = tmp * scores
        scores_ = scores - s_diag

        if mb_img is not None:

            #negative
            mb_k = self.opt.mb_k
            if im.size()[0] < mb_k: mb_k = bsize

            used_ind = torch.tensor([0 if i in indices else 1 for i in mb_ind]).bool().cuda()

            mb_img = mb_img[used_ind]
            mb_cap = mb_cap[used_ind]

            scores_img_glob = self.sim(im, mb_cap)
            i2t_k_avg = torch.exp(self.g_beta * torch.topk(scores_img_glob, mb_k)[0] - self.g_ep_nega).sum(1).reshape((bsize,1))
            i2t_k_avg_positive = torch.exp(self.g_alpha * (torch.topk(scores_img_glob, mb_k)[0] - self.g_ep_posi)).sum(1)

            scores_cap_glob = self.sim(s, mb_img)
            t2i_k_avg = torch.exp(self.g_beta * torch.topk(scores_cap_glob, mb_k)[0] - self.g_ep_nega).sum(1).reshape((1,bsize))
            t2i_k_avg_positive = torch.exp(self.g_alpha * (torch.topk(scores_cap_glob, mb_k)[0] - self.g_ep_posi)).sum(1)

            tmp_i2t = i2t_k_avg.repeat(1, bsize)
            tmp_t2i = t2i_k_avg.repeat(bsize, 1)

            exp_sii = torch.exp(self.g_beta * s_diag.sum(0))
            tmp_expii = exp_sii.reshape((bsize,1)).repeat(1, bsize)
            tmp_exptt = exp_sii.reshape((1,bsize)).repeat(bsize, 1)

            wit = (tmp_i2t + tmp_t2i) / (tmp_i2t + tmp_t2i + tmp_expii + tmp_exptt)

            #positive
            exp_sii = torch.exp(self.g_alpha * (s_diag.sum(0) - self.g_ep_posi))

            wii = 1 - exp_sii / (exp_sii + i2t_k_avg_positive + t2i_k_avg_positive)

            wit = wit - wit * tmp

            S_ = torch.exp(self.l_alpha * wit.detach() * (scores_ - self.l_ep))

            loss_diag = - torch.log(1 + F.relu((s_diag.sum(0) * wii.detach())))

        else:

            S_ = torch.exp(self.l_alpha * (scores_ - self.l_ep))

            loss_diag = - torch.log(1 + F.relu(s_diag.sum(0)))

        loss = torch.sum(
                torch.log(1 + S_.sum(0)) / self.l_alpha \
                + torch.log(1 + S_.sum(1)) / self.l_alpha \
                + loss_diag
                ) / bsize

        return loss


class VSE(object):
    """
    rkiros/uvs model
    """

    def __init__(self, opt):
        # tutorials/09 - Image Captioning
        # Build Models
        self.grad_clip = opt.grad_clip
        self.img_enc = EncoderImage(opt.data_name, opt.img_dim, opt.embed_size,
                                    opt.finetune, opt.cnn_type,
                                    use_abs=opt.use_abs,
                                    no_imgnorm=opt.no_imgnorm)
        self.txt_enc = EncoderText(opt.vocab_size, opt.word_dim,
                                   opt.embed_size, opt.num_layers,
                                   use_abs=opt.use_abs)
        if torch.cuda.is_available():
            self.img_enc.cuda()
            self.txt_enc.cuda()
            cudnn.benchmark = True

        # memory bank
        self.mb_img = None
        self.mb_cap = None
        self.mb_ind = None

        # Loss and Optimizer
        self.criterion = ContrastiveLoss(opt=opt)
        params = list(self.txt_enc.parameters())
        params += list(self.img_enc.fc.parameters())
        if opt.finetune:
            params += list(self.img_enc.cnn.parameters())
        self.params = params

        self.optimizer = torch.optim.Adam(params, lr=opt.learning_rate)

        self.Eiters = 0

    def state_dict(self):
        state_dict = [self.img_enc.state_dict(), self.txt_enc.state_dict()]
        return state_dict

    def load_state_dict(self, state_dict):
        self.img_enc.load_state_dict(state_dict[0])
        self.txt_enc.load_state_dict(state_dict[1])

    def train_start(self):
        """switch to train mode
        """
        self.img_enc.train()
        self.txt_enc.train()

    def val_start(self):
        """switch to evaluate mode
        """
        self.img_enc.eval()
        self.txt_enc.eval()

    def forward_emb(self, images, captions, lengths, volatile=False,**kwargs):
        """Compute the image and caption embeddings
        """
        # Set mini-batch dataset
        if volatile:
            with torch.no_grad():
                images = Variable(images)
                captions = Variable(captions)
        else:
            images = Variable(images)
            captions = Variable(captions)

        if torch.cuda.is_available():
            images = images.cuda()
            captions = captions.cuda()

        # Forward
        img_emb = self.img_enc(images)
        cap_emb = self.txt_enc(captions, lengths)
        return img_emb, cap_emb

    def forward_loss(self, img_emb, cap_emb, indices, **kwargs):
        """Compute the loss given pairs of image and caption embeddings
        """
        loss = self.criterion(
                img_emb,
                cap_emb,
                self.mb_img,
                self.mb_cap,
                self.mb_ind,
                indices)
        self.logger.update('Loss', loss.item(), img_emb.size(0))
        return loss

    def train_emb(self, images, captions, lengths, ids, indices, *args):
        """One training step given images and captions.
        """
        self.Eiters += 1
        self.logger.update('Eit', self.Eiters)
        self.logger.update('lr', self.optimizer.param_groups[0]['lr'])

        # compute the embeddings
        img_emb, cap_emb = self.forward_emb(images, captions, lengths)

        # measure accuracy and record loss
        self.optimizer.zero_grad()
        loss = self.forward_loss(img_emb, cap_emb, indices)

        # compute gradient and do SGD step
        loss.backward()
        if self.grad_clip > 0:
            clip_grad_norm_(self.params, self.grad_clip)
        self.optimizer.step()
