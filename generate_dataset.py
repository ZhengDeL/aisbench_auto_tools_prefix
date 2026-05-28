import logging
from transformers import AutoTokenizer
from data_picker import *
import os
import random
import torch

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def create_dataset(tokenizer_path: str, input_len: int, number: int, prefix_flag):

    logging.info(f"加载tokenizer: {tokenizer_path}")
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)

    output_samples = []
    attempts = 0
    max_attempts = number * 10  # 防止意外无限循环

    while len(output_samples) < number and attempts < max_attempts:
        attempts += 1
        # 随机选择一条文本
        picker = DataPicker("./GSM8K.jsonl", "./picked_ids.txt", prefix_flag)
        raw_text = picker.pick_one()
        if raw_text is None:
            break

        # tokenize
        tokens = tokenizer.encode(raw_text, add_special_tokens=False)

        if len(tokens) == 0:
            # logging.info(f"生成数据集失败，请清空pick ids")
            break

        # 根据需求调整长度：重复或截断
        if len(tokens) >= input_len:
            # 截断到 input_len
            adjusted_tokens = tokens[:input_len]
        else:
            # 重复整个序列直到达到 input_len
            repeat_times = (input_len + len(tokens) - 1) // len(tokens)
            repeated_tokens = tokens * repeat_times
            adjusted_tokens = repeated_tokens[:input_len]

        # 解码回文本
        adjusted_text = tokenizer.decode(adjusted_tokens, skip_special_tokens=True)
        final_len = len(tokenizer.encode(adjusted_text, add_special_tokens=False))
        if final_len != input_len:
            corrected_tokens = tokenizer.encode(adjusted_text, add_special_tokens=False)
            if len(corrected_tokens) >= input_len:
                corrected_tokens = corrected_tokens[:input_len]
            else:
                corrected_tokens = (corrected_tokens * ((input_len // len(corrected_tokens)) + 1))[:input_len]
            adjusted_text = tokenizer.decode(corrected_tokens, skip_special_tokens=True)


        output_samples.append(adjusted_text)

        # if len(output_samples) % max(1, number // 10) == 0:
        #     logging.info(f"已生成 {len(output_samples)}/{number} 条样本")

    if len(output_samples) < number:
        return None

    return output_samples


def generate_unique_tokens(tokenizer_path, seed, n, number):
    """
    根据模型 tokenizer 和随机种子，生成 n 个不相同的 token，共 number 行数据

    Args:
        model_name_or_tokenizer: 模型名称或已加载的 tokenizer 对象
        seed: 随机种子
        n: 每行需要生成的 token 数量
        number: 需要生成的数据行数

    Returns:
        list: 包含 number 行数据的列表
    """
    # 设置随机种子
    random.seed(seed)
    torch.manual_seed(seed)

    # 加载 tokenizer
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)

    # 获取词表大小
    vocab_size = len(tokenizer)

    if n > vocab_size:
        raise ValueError(f"每行请求的 token 数量 {n} 超过词表大小 {vocab_size}")

    all_lines = []

    for line_idx in range(number):
        # 为每一行使用不同的种子，确保行间数据不重复
        line_seed = seed + line_idx
        random.seed(line_seed)
        torch.manual_seed(line_seed)

        unique_tokens = []
        seen_tokens = set()
        max_attempts = n * 10  # 防止无限循环
        attempts = 0

        while len(unique_tokens) < n and attempts < max_attempts:
            # 随机生成 token ID
            token_id = random.randint(0, vocab_size - 1)

            # 检查是否重复
            if token_id in seen_tokens:
                attempts += 1
                continue

            # 转换为文本
            try:
                token_text = tokenizer.decode([token_id])

                # 可选：跳过特殊 token 或空 token
                # if token_text.strip() or token_text:  # 保留非空 token
                unique_tokens.append(token_text)
                seen_tokens.add(token_id)
            except:
                pass

            attempts += 1

        if len(unique_tokens) < n:
            print(f"警告：第 {line_idx + 1} 行只生成了 {len(unique_tokens)} 个唯一 token")
        # combined_text = ''.join(unique_tokens)
        all_lines.append(''.join(unique_tokens))

    return all_lines

def write_data(path, dataset, num):
    if num is not None:
        if len(dataset) < num:
            # 重复数据
            repeats = num // len(dataset)
            remainder = num % len(dataset)
            dataset = dataset * repeats + dataset[:remainder]
        else:
            # 截取数据
            dataset = dataset[:num]
    
    # 写入文件
    with open(path, "w", encoding="utf-8") as f:
        for item in dataset:
            f.write(json.dumps({"question": item, "answer": "none"}, ensure_ascii=False))
            f.write("\n")

def create_multi_prefix_dataset(tokenizer_path: str, input_len: int, number: int, save_path, prefix_flag, dp, repeat_rate, seed, prefix_num):
    base_name = os.path.basename(os.path.normpath(tokenizer_path))

    # 生成不带前缀数据集
    if prefix_flag == 0:
        dataset = create_dataset(tokenizer_path, input_len, number, 0)
        dataset_path = os.path.join(save_path, f'GSM8K-in{input_len}-num{number}-{base_name}.jsonl')
        write_data(dataset_path, dataset, number)
        return "", dataset_path

    # 生成前缀数据集
    prefix_len = int(input_len * repeat_rate)
    prefix_data = []
    prefix_data = create_dataset(tokenizer_path, prefix_len, prefix_num, 1)
    # if repeat_rate == 0:
    #     prefix_data = [""]
    if prefix_data == None and repeat_rate > 0:
        logging.error(f"生成数据集失败，请清空picked ids")
        exit(0)

    prefix_dataset = []
    for i in range(prefix_num):
        for j in range(dp):
            prefix_dataset.append(prefix_data[i])

    prefix_path = os.path.join(save_path, f'prefix-GSM8K-in{prefix_len}-num{dp*prefix_num}-{base_name}.jsonl')
    write_data(prefix_path, prefix_dataset, dp*prefix_num)
    if repeat_rate >= 1:
        dataset_path = os.path.join(save_path, f'GSM8K-in{prefix_len}-num{number}-{base_name}-repeatRate{repeat_rate}.jsonl')
        write_data(dataset_path, prefix_dataset, number)
        return prefix_path, dataset_path

    # 前缀后插入3个随机token
    uniq_token_set = generate_unique_tokens(tokenizer_path, seed, 3, number)
    # 后缀数据
    suffix_len = int(input_len - prefix_len - 3)
    suffix_dataset = create_dataset(tokenizer_path, suffix_len, number, 0)
    
    # 拼接完整数据集
    dataset = []
    data_len = 0
    while data_len < number:
        single_data = prefix_data[data_len % prefix_num] + uniq_token_set[data_len] + suffix_dataset[data_len]
        dataset.append(single_data)
        data_len += 1

    dataset_path = os.path.join(save_path, f'GSM8K-in{input_len}-num{number}-{base_name}-repeatRate{repeat_rate}.jsonl')
    write_data(dataset_path, dataset, number)

    return prefix_path, dataset_path

def parse_prefix_ratio(r: str) -> float:
    """
    "50%" -> 0.5, "0.5" -> 0.5, "0.500" -> 0.5
    """
    r = str(r).strip()
    if r.endswith("%"):
        v = float(r[:-1]) / 100.0
    else:
        v = float(r)
    if not (0.0 <= v <= 1.0):
        raise ValueError("prefix-ratio 必须在 [0,1] 区间或百分数 [0%,100%]")
    return v
