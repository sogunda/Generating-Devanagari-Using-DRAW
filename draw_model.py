import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.utils as vutils
import numpy as np

class DRAWModel(nn.Module):
    def __init__(self, params):
        super().__init__()

        self.T = params['T']
        self.A = params['A']
        self.B = params['B']
        self.z_size = params['z_size']
        self.N = params['N']
        self.enc_size = params['enc_size']
        self.dec_size = params['dec_size']
        self.device = params['device']

        self.cs = [0] * self.T
        
        self.logsigmas = [0] * self.T
        self.sigmas = [0] * self.T
        self.mus = [0] * self.T

        self.encoder = nn.LSTMCell(2*self.N*self.N + self.dec_size, self.enc_size)

        self.fc_mu = nn.Linear(self.enc_size, self.z_size)
        self.fc_sigma = nn.Linear(self.enc_size, self.z_size)

        self.decoder = nn.LSTMCell(self.z_size, self.dec_size)

        self.fc_write = nn.Linear(self.dec_size, self.N*self.N)

        self.fc_attention = nn.Linear(self.dec_size, 5)

    def forward(self, x):
        self.batch_size = x.size(0)

        h_enc_prev = torch.zeros(self.batch_size, self.enc_size, requires_grad=True, device=self.device)
        h_dec_prev = torch.zeros(self.batch_size, self.dec_size, requires_grad=True, device=self.device)

        enc_state = torch.zeros(self.batch_size, self.enc_size, requires_grad=True, device=self.device)
        dec_state = torch.zeros(self.batch_size, self.dec_size, requires_grad=True, device=self.device)

        for t in range(self.T):
            c_prev = torch.zeros(self.batch_size, self.B*self.A, requires_grad=True, device=self.device) if t == 0 else self.cs[t-1]
            x_hat = x - F.sigmoid(c_prev)

            r_t = self.read(x, x_hat, h_dec_prev)

            h_enc, enc_state = self.encoder(torch.cat((r_t, h_dec_prev), dim=1), (h_enc_prev, enc_state))

            z, self.mus[t], self.logsigmas[t], self.sigmas[t] = self.sampleQ(h_enc)

            h_dec, dec_state = self.decoder(z, (h_dec_prev, dec_state))

            self.cs[t] = c_prev + self.write(h_dec)

            h_enc_prev = h_enc
            h_dec_prev = h_dec

    def read(self, x, x_hat, h_dec_prev):
        # Using attention
        Fx, Fy, gamma = self.attn_window(h_dec)

        def filter_img(img, Fx, Fy, gamma):
            Fxt = Fx.transpose(2, 1)
            img = img.vie(-1, self.B, self.A)
            glimpse = Fy.bmm(img.bmm(Fxt))
            glimpse = glimpse.view(-1, self.N*self.N)

            return glimpse * gamma.view(-1, 1).expand_as(glimpse)

        x = filter_img(x, Fx, Fy, gamma)
        x_hat = filter_img(x_hat, Fx, Fy, gamma)

        return torch.cat((x, x_hat), dim=1)
        # No attention
        #return torch.cat((x, x_hat), dim=1)

    def write(self, h_dec):
        # Using attention
        w = self.fc_write(h_dec)
        w = w.view(self.batch_size, self.N, self.N)

        Fx, Fy, gamma = self.attn_window(h_dec)
        Fyt = Fy.transpose(2, 1)

        wr = Fyt.bmm(w.bmm(Fx))
        wr = wr.view(self.batch_size, self.B*self.A)

        return wr / gamma.view(-1, 1).expand_as(wr)
        # No attention
        #return self.fc_write(h_dec)

    def sampleQ(self, h_enc):
        e = torch.randn(self.batch_size, self.z_size, device=self.device)

        mu = self.fc_mu(h_enc)
        log_sigma = self.fc_sigma(h_enc)

        sigma = torch.exp(log_sigma)
        z = mu + e * sigma

        return z, mu, log_sigma, sigma

    def attn_window(self, h_dec):
        params = self.fc_attention(h_dec)
        gx_, gy_, log_sigma_2, log_delta_, log_gamma = params.split(1, 1)

        gx = (self.A + 1) / 2 * (gx_ + 1)
        gy = (self.B + 1) / 2 * (gy_ + 1)
        delta = (max(self.A, self.B) - 1) / (self.N - 1) * torch.exp(log_delta_)
        sigma_2 = torch.exp(log_sigma_2)
        gamma = torch.exp(log_gamma)

        return self.filterbank(gx, gy, sigma_2, delta), gamma

    def filterbank(self, gx, gy, sigma_2, delta, epsilon=1e-8):
        grid_i = torch.arange(0, self.N, device=self.device).view(1, -1)
        
        mu_x = gx + (grid_i - self.N / 2 - 0.5) * delta
        mu_y = gy + (grid_i - self.N / 2 - 0.5) * delta

        a = tf.arange(0, self.A, device=self.device).view(1, 1, -1)
        b = tf.arange(0, self.B, device=self.device).view(1, 1, -1)

        mu_x = mu_x.view(-1, self.N, 1)
        mu_y = mu_y.view(-1, self.N, 1)
        sigma_2 = sigma_2.view(-1, 1, 1)

        Fx = torch.exp(-torch.pow(a - mu_x, 2) / (2 * sigma_2))
        Fy = torch.exp(-torch.pow(b - mu_y, 2) / (2 * sigma_2))

        Fx = Fx / (Fx.sum(2, True).expand_as(Fx) + epsilon)
        Fy = Fy / (Fy.sum(2, True).expand_as(Fy) + epsilon)

        return Fx, Fy

    def loss(self, x):
        self.forward(x)

        criterion = nn.BCELoss()
        x_recon = F.sigmoid(self.cs[-1])
        # Only want to average across the mini-batch, hence, multiply by the image dimensions.
        Lx = criterion(x_recon, x) * self.A * self.B

        Lz = 0

        for t in range(self.T):
            mu_2 = self.mus[t] * self.mus[t]
            sigma_2 = self.sigmas[t] * self.sigmas[t]
            logsigma = self.logsigmas[t]

            kl_loss = 0.5*torch.sum(mu_2 + sigma_2 - 2*logsigma, 1) - 0.5*self.T
            Lz += kl_loss

        Lz = torch.mean(Lz)
        net_loss = Lx + Lz

        return net_loss

    def generate(self, num_output):
        h_dec_prev = torch.zeros(num_output, self.dec_size, device=self.device)
        dec_state = torch.zeros(num_output, self.dec_size  , device=self.device)

        for t in range(self.T):
            c_prev = torch.zeros(num_output, self.B*self.A, device=self.device) if t == 0 else self.cs[t-1]
            z = torch.randn(num_output, self.z_size, device=self.device)
            h_dec, dec_state = self.decoder(z, (h_dec_prev, dec_state))
            self.cs[t] = c_prev + self.write(h_dec)
            h_dec_prev = h_dec

        imgs = []

        for img in self.cs:
            img = img.view(-1, 1, self.B, self.A)
            imgs.append(vutils.make_grid(F.sigmoid(img).detach().cpu(), nrow=int(np.sqrt(int(num_output))), padding=2, normalize=True))

        return imgs