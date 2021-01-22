import argparse
import numpy as np

import gym
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Beta
from torch.utils.data.sampler import BatchSampler , SubsetRandomSampler
from utils import DrawLine

parser = argparse.ArgumentParser(description="PPO Agent for Car Racing")
#PPO = Proximal policy optimization
parser.add_argument('--gamma',type=float, default=0.99, metavar="G", help='Discount factor (defailt : 0.99)')
parser.add_argument('--action-repeat', type=int, default=8, metavar='N', help="Repeat action in N frames (default:8)")
parser.add_argument('--img-stack',type=int,default=4,metavar="N", help="stack N image in a state (default: 4)")
parser.add_argument('--seed',type=int, default=0, metavar='N', help='random seed (default:0)')
parser.add_argument('--render',action='store_true',help= 'render the environment')
parser.add_argument('--vis', action='store_true', help='use visdom for rendering')
parser.add_argument("--log-interval", type=int, default=10, metavar='N',
    help="interval training status logs (default:10)")

args = parser.parse_args()

use_cuda = torch.cuda.is_available()
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
if use_cuda:
    torch.manual_seed(args.seed)

transition = np.dtype([('s',np.float64, (args.img_stack, 96, 96)),('a',np.float64, (3,)), ('a_logp', np.float64) ,
                        ('r',np.float64), ('s_', np.float64, (args.img_stack, 96, 96))])


class Env():

    def __init__(self):
        self.env = gym.make('CarRacing-v0')
        self.env.seed(args.seed)
        self.reward_threshold = self.env.spec.reward_threshold

    def reset(self):
        self.counter = 0
        self.av_r = self.reward_memory()
        self.die=False
        img_rgb = self.env.reset()
        img_gray = self.rgb2gray(img_rgb)
        self.stack = [img_gray]*args.img_stack #FOUR FRAMES FOR DECISION
        return np.array(self.stack)

    def step(self, action):
        total_reward = 0
        
        for i in range(args.action_repeat):
            img_rgb , reward ,die, _  = self.env.step(action)
            
            #do not penalize in "die state"
            if die:
                reward+=100
            #green penalty
            if np.mean(img_rgb[:,:,1]) > 185.0:
                reward -= 0.05
            
            total_reward+= reward
            #if no reward recently end the episode
            done = True if self.av_r(reward)<= -0.1 else False

            if done or die:
                break

        img_gray = self.rgb2gray(img_rgb)
        self.stack.pop(0)
        self.stack.append(img_gray)

        assert len(self.stack) == args.img_stack

        return np.array(self.stack), total_reward, done, die

    def render(self, *arg):
        self.env.render(*arg)

    @staticmethod
    def rgb2gray(rgb, norm=True):
        gray = np.dot(rgb[...,:],[0.299, 0.587, 0.114])

        if norm:
            gray = gray/128.0 -1.0

        return gray

    @staticmethod
    def reward_memory():
        #record reward for last 100 steps
        count = 0
        length = 100
        history = np.zeros(length)

        def memory(reward):
            nonlocal count
            history[count] = reward
            count = (count+1)%length
            return np.mean(history)

        return memory

class Net(nn.Module):
    """
    Actor-critic network
    """
    def __init__(self):
        super(Net, self).__init__()
        self.cnn_base = nn.Sequential(#input shape = (4,96,96)
            nn.Conv2d(args.img_stack, 8, kernel_size= 4 ,stride=2),
            nn.ReLU(),
            nn.Conv2d(8, 16, kernel_size=3, stride=2), # (8,47,47)
            nn.ReLU(),
            nn.Conv2d(16, 32, kernel_size=3, stride=2), #(16, 23, 23)
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=3, stride=2), # (32, 11, 11)
            nn.ReLU(),
            nn.Conv2d(64, 128, kernel_size=3, stride=1), # (64, 5, 5)
            nn.ReLU(),
            nn.Conv2d(128, 256, kernel_size=3, stride=1), # (128, 3, 3)
            nn.ReLU(),
        ) #output shape (256, 1, 1)

        self.v = nn.Sequential(nn.Linear(256,100), nn.ReLU(), nn.Linear(100,1))
        self.fc = nn.Sequential(nn.Linear(256,100), nn.ReLU())
        self.alpha_head = nn.Sequential(nn.Linear(100,3), nn.Softplus())
        self.beta_head = nn.Sequential(nn.Linear(100,3), nn.Softplus())
        self.apply(self.__weights_init)

    @staticmethod
    def __weights_init(m):
        if isinstance(m, nn.Conv2d):
            nn.init.xavier_uniform_(m.weight, gain=nn.init.calculate_gain('relu'))
            nn.init.constant(m.bias, 0.1)

    def forward(self,x):
        x = self.cnn_base(x)
        x = x.view(-1,256)
        v = self.v(x)
        x = self.fc(x)
        alpha = self.alpha_head(x) + 1
        beta = self.beta_head(x) + 1

        return (alpha, beta), v


class Agent():
    
    max_grad_norm = 0.5
    clip_param = 0.1
    ppo_epoch = 10
    buffer_capacity, batch_size = 2000, 120

    def __init__(self):
        self.training_step = 0
        self.net = Net().double().to(device)
        self.buffer = np.empty(self.buffer_capacity, dtype= transition)
        self.counter = 0
        self.optimizer = optim.Adam(self.net.parameters(), lr=1e-3)

    def select_action(self,state):
        state = torch.from_numpy(state).double().to(device).unsqueeze(0)
        with torch.no_grad():
            alpha, beta = self.net(state)[0]

        dist = Beta(alpha, beta)
        action = dist.sample()
        a_logp = dist.log_prob(action).sum(dim=1)

        action = action.squeeze().cpu().numpy()
        a_logp= a_logp.item()

        return action, a_logp

    def save_param(self):
        torch.save(self.net.state_dict(), 'param/ppo_net_params.pkl')

    def store(self, transition):
        self.buffer[self.counter] = transition
        self.counter += 1
        if self.counter == self.buffer_capacity:
            self.counter = 0
            return True
        else:
            return False

    def update(self):
        self.training_step+=1

        s = torch.tensor(self.buffer['s'], dtype=torch.double).to(device)
        a = torch.tensor(self.buffer['a'], dtype=torch.double).to(device)
        r = torch.tensor(self.buffer['r'], dtype=torch.double).to(device).view(-1,1)
        s_ = torch.tensor(self.buffer['s_'], dtype=torch.double).to(device)

        old_a_logp = torch.tensor(self.buffer['a_logp'], dtype= torch.double).to(device).view(-1,1)

        with torch.no_grad():
            target_v = r + args.gamma*self.net(s_)[1]
            adv = target_v - self.net(s)[1]

        for _ in range(self.ppo_epoch):
            for idx in BatchSampler(SubsetRandomSampler(range(self.buffer_capacity)), self.batch_size, False):

                alpha, beta = self.net(s[idx])[0]
                dist = Beta(alpha, beta)
                a_logp = dist.log_prob(a[idx]).sum(dim=1, keepdim=True)
                ratio = torch.exp(a_logp - old_a_logp[idx])

                sur1 = ratio*adv[idx]
                sur2 = torch.clamp(ratio, 1.0 - self.clip_param, 1.0 + self.clip_param)* adv[idx]
                action_loss -= torch.min(sur1, sur2).mean()
                value_loss = F.smooth_l1_loss(self.net(s[idx])[1], target_v[idx])
                loss = action_loss + 2. * value_loss

                self.optimizer.zero_grad()
                loss.backward()

                self.optimizer.step()

if __name__=="__main__":
    agent = Agent()
    env = Env()
    if args.vis:
        draw_reward = DrawLine(env="car", title= "PPO", xlabel="Episode", ylabel="Moving averaged episode reward")

    training_records = []
    running_score = 0

    state = env.reset()
    
    for i_ep in range(100000):
        score = 0
        state = env.reset()

        for t in range(1000):
            action, a_logp = agent.select_action(state)
            state_, reward, done, die = env.step(action*np.array([2.,1.,1.]) + np.array([-1., 0., 0.]))
        
            if args.render:
                env.render()

            if agent.store((state, action, a_logp, reward, state_)):
                print("Updating")
                agent.update()
        
            score += reward
            state = state_

            if done or die:
                break

        running_score = running_score * 0.99 + score * 0.01

        if i_ep % args.log_interval == 0:
            if args.vis:
                draw_reward(xdata=i_ep, ydata=running_score)
            print(f"Ep {i_ep}\t Last score : {score:.2f}\tMoving average score :{running_score:.2f}")
            
            agent.save_param()

        if running_score > env.reward_threshold:
            print("Solved ! Running Reward is now{running score} amd the last episode runs to {score} !")
            break