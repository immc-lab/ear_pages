import os
import csv
import torch
from PIL import Image
from tqdm import tqdm
from janus.models import MultiModalityCausalLM, VLChatProcessor
import PIL.Image
import numpy as np
import pandas as pd
from FInrTuner import FineTunedModel
import random
import argparse
import glob
import sys
import json


def seed_all(seed=42):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


seed_all(42)


def create_conversation(prompt):
    return [
        {
            "role": "<|User|>",
            "content": prompt,
        },
        {"role": "<|Assistant|>", "content": ""},
    ]


def load_janus_model(model_path):
    vl_chat_processor = VLChatProcessor.from_pretrained(model_path)
    model = MultiModalityCausalLM.from_pretrained(
        model_path,
        trust_remote_code=True
    )
    model = model.to(torch.bfloat16).cuda().eval()
    return vl_chat_processor, model


@torch.inference_mode()
def generate(
        mmgpt: MultiModalityCausalLM,
        vl_chat_processor: VLChatProcessor,
        prompt: str,
        save_dir: str,
        temperature: float = 1,
        parallel_size: int = 1,
        cfg_weight: float = 5,
        image_token_num_per_image: int = 576,
        img_size: int = 384,
        patch_size: int = 16,
        index: int = 0,
):
    input_ids = vl_chat_processor.tokenizer.encode(prompt)
    input_ids = torch.LongTensor(input_ids)

    tokens = torch.zeros((parallel_size * 2, len(input_ids)), dtype=torch.int).cuda()
    for i in range(parallel_size * 2):
        tokens[i, :] = input_ids
        if i % 2 != 0:
            tokens[i, 1:-1] = vl_chat_processor.pad_id

    inputs_embeds = mmgpt.language_model.get_input_embeddings()(tokens)
    generated_tokens = torch.zeros(
        (parallel_size, image_token_num_per_image), dtype=torch.int
    ).cuda()

    for i in range(image_token_num_per_image):
        outputs = mmgpt.language_model.model(
            inputs_embeds=inputs_embeds,
            use_cache=True,
            past_key_values=outputs.past_key_values if i != 0 else None,
        )
        hidden_states = outputs.last_hidden_state

        logits = mmgpt.gen_head(hidden_states[:, -1, :])
        logit_cond = logits[0::2, :]
        logit_uncond = logits[1::2, :]

        logits = logit_uncond + cfg_weight * (logit_cond - logit_uncond)
        probs = torch.softmax(logits / temperature, dim=-1)

        next_token = torch.multinomial(probs, num_samples=1)
        generated_tokens[:, i] = next_token.squeeze(dim=-1)

        next_token = torch.cat(
            [next_token.unsqueeze(dim=1), next_token.unsqueeze(dim=1)], dim=1
        ).view(-1)
        img_embeds = mmgpt.prepare_gen_img_embeds(next_token)
        inputs_embeds = img_embeds.unsqueeze(dim=1)

    dec = mmgpt.gen_vision_model.decode_code(
        generated_tokens.to(dtype=torch.int),
        shape=[parallel_size, 8, img_size // patch_size, img_size // patch_size],
    )
    dec = dec.to(torch.float32).cpu().numpy().transpose(0, 2, 3, 1)
    dec = np.clip((dec + 1) / 2 * 255, 0, 255)
    visual_img = np.zeros((parallel_size, img_size, img_size, 3), dtype=np.uint8)
    visual_img[:, :, :] = dec

    os.makedirs(save_dir, exist_ok=True)
    for i in range(parallel_size):
        save_path = os.path.join(save_dir, f"{index}.png")
        PIL.Image.fromarray(visual_img[i]).save(save_path)


def main(json_path, output_dir, model_path, finetune_path=None):
    os.makedirs(output_dir, exist_ok=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor, model = load_janus_model(model_path)

    if finetune_path:
        finetuner = FineTunedModel(model.language_model.model, num_layers=5)
        finetuner.load_state_dict(torch.load(finetune_path))

    model.to(device)

    with open(json_path, 'r') as f:
        prompts = json.load(f)

    for idx, item in tqdm(enumerate(prompts)):
        try:
            save_path = os.path.join(output_dir, f"{idx}.png")
            if os.path.exists(save_path):
                print(f"The image already exists, skip:{save_path}")
                continue

            conversation = create_conversation(item["prompt"])

            sft_format = processor.apply_sft_template_for_multi_turn_prompts(
                conversations=conversation,
                sft_format=processor.sft_format,
                system_prompt="",
            )
            final_prompt = sft_format + processor.image_start_tag

            if finetune_path:
                with finetuner:
                    generate(
                        model,
                        processor,
                        final_prompt,
                        save_dir=output_dir,
                        index=idx
                    )
            else:
                generate(
                    model,
                    processor,
                    final_prompt,
                    save_dir=output_dir,
                    index=idx
                )
            print(f"Generated images for prompt: {item['prompt']} -> saved to {output_dir}")

        except Exception as e:
            print(f"Generation failed: {idx} prompt '{item['prompt']} '. Error:{str(e)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Image generation config")
    parser.add_argument("--json_path", default="../data/test_church.json",
                        help="JSON file path")
    parser.add_argument("--model_path", default="/path/to/huggingface/Janus-Pro-7B",
                        help="Basic model path")
    parser.add_argument("--output_dir",
                        default="/path/to/save_path/ear_church",
                        help="Output directory path")
    parser.add_argument("--finetune_path",
                        default="/path/to/save_path/church/ft_model_ear_church.pt",
                        help="Fine tune the model pt file path")

    args = parser.parse_args()

    config = {
        "json_path": args.json_path,
        "output_dir": args.output_dir,
        "model_path": args.model_path,
        "finetune_path": args.finetune_path
    }

    os.makedirs(args.output_dir, exist_ok=True)

    print("===== config =====")
    for key, value in config.items():
        print(f"{key}: {value}")
    print("===================")

    main(**config)

    print("\n===== start eval =====")
    eval_command = f"python ../eval/eval_object.py --folder_path {args.output_dir} --topk 10 --batch_size 250"
    print(f"Execute command: {eval_command}")
    os.system(eval_command)
    print("===== eval completed =====")

    print("\n===== Statistical classification results =====")
    results_file = os.path.join(args.output_dir, "classification_results.csv")
    if os.path.exists(results_file):
        import pandas as pd

        df = pd.read_csv(results_file)
        total_count = len(df)
        church_count = len(df[df['category_top1'] == 'church'])
        church_rate = church_count / total_count * 100

        print(f"total data: {total_count}")
        print(f"data classified as church: {church_count}")
        print(f"church classification rate: {church_rate:.2f}%")
    else:
        print(f"Error: Classification result file not found {results_file}")
    print("=======================")
