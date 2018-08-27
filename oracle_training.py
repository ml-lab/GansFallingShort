import argparse
import pdb
import numpy as np
import torch
import torch.utils.data
import torch.optim as optim
import tensorboardX

from utils  import * 
from data   import * 
from models import * 
from losses import * 
from args   import * 


def main(rlm=False, rlm_dir=None):

    args = get_train_args()
    assert 'synthetic' in args.base_dir, 'make sure you are logging correctly'

    # reproducibility
    torch.manual_seed(2)
    np.random.seed(2)

    # add extra args
    args.vocab_size = 5000
    args.num_oracle_samples = 10000
    args.num_oracle_samples_test = 5000
    args.cuda = False if args.no_cuda else True

    # Logging
    maybe_create_dir(args.base_dir)
    maybe_create_dir(os.path.join(args.base_dir, 'models'))
    print_and_save_args(args, args.base_dir)
    writer = tensorboardX.SummaryWriter(log_dir=os.path.join(args.base_dir, 'TB'))
    writes = 0
    best_valid, best_test = 1e5, 1e5

    gen  = Generator(args)
    disc = Discriminator(args)
    oracle = get_oracle(args)

    if args.cuda: 
        gen  = gen.cuda()
        disc = disc.cuda()
        oracle = oracle.cuda()

    optimizer_gen    = optim.Adam(gen.parameters(),         lr=args.gen_lr)
    optimizer_critic = optim.Adam(disc.critic.parameters(), lr=args.critic_lr)
    optimizer_disc   = optim.Adam([p for (n,p) in disc.named_parameters() if 'critic' not in n], lr=args.disc_lr)

    # makes logging easier
    MODELS = [ ('gen', gen, optimizer_gen), ('disc', disc, optimizer_disc), ('critic', None, optimizer_critic)]

    # we first create the synthetic dataset
    start_token = Variable(torch.zeros(args.batch_size, 1)).long().cuda() 
    if args.cuda: start_token = start_token.cuda()

    sentences = []
    for _ in range((args.num_oracle_samples + args.num_oracle_samples_test) // args.batch_size + 1):
        _, oracle_data = oracle(start_token)
        sentences += [oracle_data]

    sentences = torch.cat(sentences, dim=0).cpu().data.numpy()
    dataset_train = sentences[:args.num_oracle_samples]
    dataset_test  = sentences[args.num_oracle_samples:args.num_oracle_samples+args.num_oracle_samples_test]
    train_loader = torch.utils.data.DataLoader(dataset_train, batch_size=args.batch_size, shuffle=True)
    test_loader  = torch.utils.data.DataLoader(dataset_test,   batch_size=1000, shuffle=False)

    '''
    MLE pretraining
    '''
    for epoch in range(args.mle_epochs):
        print('MLE pretraining epoch {}/{}'.format(epoch, args.mle_epochs))
        losses_train, losses_dev, oracle_nlls = [], [], []
        gen.train()

        # Training loop
        for i, minibatch in enumerate(train_loader):
            input  = torch.cat([start_token, minibatch[:, :-1]], dim=1)
            target = minibatch
 
            gen_logits, _ = gen(input)
            loss = F.cross_entropy(gen_logits.view(-1, gen_logits.size(-1)), target.flatten())
            losses_train += [loss.data]
            apply_loss(optimizer_gen, loss, clip_norm=args.grad_clip)
        
        print_and_log_scalar(writer, 'train/nll', losses_train, writes, end_token='\n')

        if (epoch + 1) % args.test_every == 0 and False:
            for split in ['valid','test']:
                dataset = dataset_valid if split=='valid' else dataset_test
                loader_dev  = minibatch_generator(dataset,  args, shuffle=False)
                with torch.no_grad():
                    gen.eval()

                    # Test loop
                    for i, minibatch in enumerate(loader_dev):
                        input, target, lens = minibatch

                        gen_logits, _ = gen(input)
                        loss = masked_cross_entropy(gen_logits, target, lens)
                        losses_dev += [loss.data]

                        if args.lm_path: 
                            # generate a sentence, a sentence, and feed to oracle lm
                            gen_logits, gen_sample = gen(input[:, [0]])
                            oracle_input = torch.cat([input[:, [0]], gen_sample], dim=1)
                            oracle_logits, _ = oracle_lm(oracle_input.detach())
                        
                            nll = NLL(oracle_logits[:, :-1], gen_sample)
                            oracle_nlls += [nll.data] 

                    print_and_log_scalar(writer, '{}/oracle_nll'.format(split), oracle_nlls, writes)
                    print_and_log_scalar(writer, '{}/nll'.format(split), losses_dev, writes, end_token='\n')

                    # keep tab of best valid error in order to get legit test error:
                    if split == 'valid':
                        curr_valid_loss = np.mean(losses_dev)
                        best_valid = min(best_valid,curr_valid_loss)
                    if split == 'test':
                        best_test = np.mean(losses_dev) if best_valid==curr_valid_loss else best_test
                        
        writes += 1
           
        # save samples
        gen.eval()
        fake_logits, fake_sentences = gen(input[:, [0]])
        print_and_save_samples(fake_sentences, word_dict, args.base_dir, epoch, char_level=args.character_level)

        if (epoch + 1) % args.save_every == 0: 
            save_models(MODELS[0:1], args.base_dir, writes)

    # if in rlm mode, store the rlm_score
    if rlm:
        return best_test

    if args.transfer_weights_after_pretraining and args.mle_epochs > 0:
        transfer_weights(gen, disc)
        print('transfered weights from generator to discriminator')


    '''
    Adversarial training
    '''
    for epoch in range(args.adv_epochs):
        print('ADV training epoch {}'.format(epoch))
        train_loader = minibatch_generator(dataset_train, args, shuffle=True)
        gen_losses, disc_losses, critic_losses, ps_real, ps_fake, real_accs, fake_accs, nlls = \
                [[] for _ in range(8)]
        gen.train(); disc.train()

        # Training loop
        for i, minibatch in enumerate(train_loader):
            input, target, lens = minibatch
            should_train_gen, should_train_disc, should_train_mle = assign_training(i, epoch, args)

            if should_train_disc:
                # train disc on real data
                real_out, _  = disc(target)
                real_loss = F.binary_cross_entropy_with_logits(real_out, torch.ones_like(real_out))
                p_real = F.sigmoid(real_out)
                real_acc = (p_real[:, -1] > 0.5).type(torch.float).mean().data
                p_real = p_real.mean().data
                ps_real += [p_real]
                real_accs += [real_acc]
                               
                # train disc on fake data
                _, fake_sentences = gen(input[:, [0]])
                fake_out, fake_baseline = disc(fake_sentences.detach())
                fake_loss = F.binary_cross_entropy_with_logits(fake_out, torch.zeros_like(fake_out))
                p_fake = F.sigmoid(fake_out)
                fake_acc = (p_fake[:, -1] < 0.5).type(torch.float).mean().data
                p_fake = p_fake.mean().data
                ps_fake += [p_fake]
                fake_accs += [fake_acc]
                disc_loss = (fake_loss + real_loss) / 2
                disc_losses += [disc_loss.data]

                apply_loss(optimizer_disc, disc_loss, clip_norm=args.grad_clip)
                
                # train critic
                if args.use_baseline: 
                    cumulative_rewards = get_cumulative_rewards(fake_out, args)
                    critic_loss = reinforce_critic_loss(cumulative_rewards, fake_baseline)
                    critic_losses += [critic_loss.data]            

                    apply_loss(optimizer_critic, critic_loss, clip_norm=args.grad_clip)
            
            if should_train_gen:
                # train generator
                fake_logits, fake_sentence = gen(input[:, [0]])
                fake_out, fake_baseline = disc(fake_sentence.detach())
                cumulative_rewards = get_cumulative_rewards(fake_out, args)
                gen_loss = reinforce_gen_loss(cumulative_rewards, fake_logits, fake_sentence, 
                                              fake_baseline, args)
                gen_losses += [gen_loss.data]

                apply_loss(optimizer_gen, gen_loss, clip_norm=args.grad_clip)

            if should_train_mle:
                fake_logits, _  = gen(input)
                nll = masked_cross_entropy(fake_logits, target, lens)
                nlls += [nll.data]
                
                apply_loss(optimizer_gen, nll, clip_norm=args.grad_clip)
            

        # logging
        print_and_log_scalar(writer, 'train/P(real)', ps_real, writes)      
        print_and_log_scalar(writer, 'train/real Accuracy', real_accs, writes)
        print_and_log_scalar(writer, 'train/P(fake)', ps_fake, writes)      
        print_and_log_scalar(writer, 'train/fake Accuracy', fake_accs, writes)
        print_and_log_scalar(writer, 'train/nll', nlls, writes)      
        print_and_log_scalar(writer, 'train/Gen Loss', gen_losses, writes)      
        print_and_log_scalar(writer, 'train/Disc Loss', disc_losses, writes)      
        print_and_log_scalar(writer, 'train/Critic Loss', critic_losses, writes, end_token='\n')      


        if (epoch + 1) % args.test_every == 0: 
            valid_loader  = minibatch_generator(dataset_valid,  args, shuffle=False)
            with torch.no_grad():
                gen_losses, disc_losses, critic_losses, ps_real, ps_fake, real_accs, \
                    fake_accs, nlls, oracle_nlls, mixed_nlls = [[] for _ in range(10)]
                gen.eval(); disc.eval()

                # Test loop
                for i, minibatch in enumerate(valid_loader):
                    input, target, lens = minibatch
                    
                    # disc on real data
                    real_out, _  = disc(target)
                    real_loss = F.binary_cross_entropy_with_logits(real_out, torch.ones_like(real_out))
                    p_real = F.sigmoid(real_out)
                    real_acc = (p_real[:, -1] >0.5).type(torch.float).mean().data
                    p_real = p_real.mean().data
                    ps_real += [p_real]
                    real_accs += [real_acc]
                    
                                   
                    # disc on fake data
                    _, fake_sentences = gen(input[:, [0]])
                    fake_out, fake_baseline = disc(fake_sentences.detach())
                    fake_loss = F.binary_cross_entropy_with_logits(fake_out, torch.zeros_like(fake_out))
                    p_fake = F.sigmoid(fake_out)
                    fake_acc = (p_fake[:, -1] <0.5).type(torch.float).mean().data
                    p_fake = p_fake.mean().data
                    ps_fake += [p_fake]
                    fake_accs += [fake_acc]
                    disc_loss = (fake_loss + real_loss) / 2
                    disc_losses += [disc_loss.data]
                    
                    # critic
                    if args.use_baseline: 
                        cumulative_rewards = get_cumulative_rewards(fake_out, args)
                        critic_loss = reinforce_critic_loss(cumulative_rewards, fake_baseline)
                        critic_losses += [critic_loss.data]            
                      
                    # generator in free sampling mode
                    fake_logits, fake_sentence = gen(input[:, [0]])
                    fake_out, fake_baseline = disc(fake_sentence.detach())
                    cumulative_rewards = get_cumulative_rewards(fake_out, args)
                    gen_loss = reinforce_gen_loss(cumulative_rewards, fake_logits, fake_sentence, 
                                                  fake_baseline, args)
                    gen_losses += [gen_loss.data]

                    # generator in teacher forcing mode
                    fake_logits, _  = gen(input)
                    nll = masked_cross_entropy(fake_logits, target, lens)
                    nlls += [nll.data]
                    
                    if args.lm_path: 
                        # generate a sentence, a sentence, and feed to oracle lm
                        oracle_input = torch.cat([input[:, [0]], fake_sentence], dim=1)
                        oracle_logits, _ = oracle_lm(oracle_input.detach())
                    
                        oracle_nll = NLL(oracle_logits[:, :-1], fake_sentence)
                        oracle_nlls += [oracle_nll.data] 
                        
                        mixed_nlls += [(nll.data+oracle_nll.data)/2]
                    
            
                # logging
                print_and_log_scalar(writer, 'valid/oracle_nll', oracle_nlls, writes)
                print_and_log_scalar(writer, 'valid/P(real)', ps_real, writes)
                print_and_log_scalar(writer, 'valid/real Accuracy', real_accs, writes)
                print_and_log_scalar(writer, 'valid/P(fake)', ps_fake, writes)
                print_and_log_scalar(writer, 'valid/fake Accuracy', fake_accs, writes)
                print_and_log_scalar(writer, 'valid/nll', nlls, writes)
                print_and_log_scalar(writer, 'valid/mixed nll', mixed_nlls, writes)
                print_and_log_scalar(writer, 'valid/Gen Loss', gen_losses, writes)      
                print_and_log_scalar(writer, 'valid/Disc Loss', disc_losses, writes)      
                print_and_log_scalar(writer, 'valid/Critic Loss', critic_losses, writes, end_token='\n')      
                
        writes += 1

        # save samples
        gen.eval()
        fake_logits, fake_sentences = gen(input[:, [0]])
        print_and_save_samples(fake_sentences, word_dict, args.base_dir, epoch, char_level=args.character_level)

        # save models
        if (epoch + 1) % args.save_every == 0: 
            save_models(MODELS, args.base_dir, writes)


if __name__ == '__main__':
    main()

