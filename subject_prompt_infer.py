import argparse
import os
import sys

from utils.config import Config
from utils.registry_class import INFER_ENGINE
from tools import *


def build_test_list(subject_image, prompt, output_list):
    img_name = os.path.basename(subject_image)
    if '*' not in prompt:
        prompt = prompt.strip() + ' *'
    line = f"{img_name}|||{prompt}"
    with open(output_list, 'w') as f:
        f.write(line + "\n")
    return output_list


def main():
    parser = argparse.ArgumentParser(description="DreamVideo subject-only inference")
    parser.add_argument("--cfg", default="configs/dreamvideo/infer/examples/subject_dog2.yaml")
    parser.add_argument("--subject_image", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output_list", default="data/custom/infer/subject_prompt_runtime.txt")
    parser.add_argument("--log_dir", default=None)
    args = parser.parse_args()

    subject_image = os.path.abspath(args.subject_image)
    if not os.path.exists(subject_image):
        raise FileNotFoundError(f"Subject image not found: {subject_image}")

    test_data_dir = os.path.dirname(subject_image)
    output_list = os.path.abspath(args.output_list)
    os.makedirs(os.path.dirname(output_list), exist_ok=True)

    build_test_list(subject_image, args.prompt, output_list)

    sys.argv = ["subject_prompt_infer.py", "--cfg", args.cfg]
    cfg_update = Config(load=True)

    cfg_update.cfg_dict["test_list_path"] = output_list
    cfg_update.cfg_dict["test_data_dir"] = test_data_dir
    if args.log_dir:
        cfg_update.cfg_dict["log_dir"] = args.log_dir

    INFER_ENGINE.build(dict(type=cfg_update.TASK_TYPE), cfg_update=cfg_update.cfg_dict)


if __name__ == "__main__":
    main()
