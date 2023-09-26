import kfp
import kfp.dsl as dsl
from kfp.dsl import component
from kfp.compiler import Compiler
from kubernetes import client, config


# @component()
# def download_data(
#     dataset: Output[Dataset]
# ):
#     print("download_dataset")

@component(
    packages_to_install=["transformers", "peft", "torch", "datasets", "os", "tqdm"],
    base_image='python:3.10'
)
def prompt_tuning_bloom(peft_model_publish_id: str, model_name_or_path: str, num_epochs: int):
    from transformers import AutoModelForCausalLM, AutoTokenizer, default_data_collator, get_linear_schedule_with_warmup
    from peft import get_peft_config, get_peft_model, PromptTuningInit, PromptTuningConfig, TaskType, PeftType
    import torch
    from datasets import load_dataset
    import os
    from torch.utils.data import DataLoader
    from tqdm import tqdm

    import os
    print("Diana : printin env variable")
    print(os.environ.get('HUGGINGFACE_TOKEN'))

    peft_config = PromptTuningConfig(
        task_type=TaskType.CAUSAL_LM,
        prompt_tuning_init=PromptTuningInit.TEXT,
        num_virtual_tokens=8,
        prompt_tuning_init_text="Classify if the tweet is a complaint or not:",
        tokenizer_name_or_path=model_name_or_path,
    )

    dataset_name = "twitter_complaints"
    text_column = "Tweet text"
    label_column = "text_label"
    max_length = 64
    lr = 3e-2
    batch_size = 8

    dataset = load_dataset("ought/raft", dataset_name)
    dataset["train"][0]

    classes = [k.replace("_", " ") for k in dataset["train"].features["Label"].names]
    dataset = dataset.map(
        lambda x: {"text_label": [classes[label] for label in x["Label"]]},
        batched=True,
        num_proc=1,
    )

    tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    def preprocess_function(examples):
        batch_size = len(examples[text_column])
        inputs = [f"{text_column} : {x} Label : " for x in examples[text_column]]
        targets = [str(x) for x in examples[label_column]]
        model_inputs = tokenizer(inputs)
        labels = tokenizer(targets)
        for i in range(batch_size):
            sample_input_ids = model_inputs["input_ids"][i]
            label_input_ids = labels["input_ids"][i] + [tokenizer.pad_token_id]
            model_inputs["input_ids"][i] = sample_input_ids + label_input_ids
            labels["input_ids"][i] = [-100] * len(sample_input_ids) + label_input_ids
            model_inputs["attention_mask"][i] = [1] * len(model_inputs["input_ids"][i])
        for i in range(batch_size):
            sample_input_ids = model_inputs["input_ids"][i]
            label_input_ids = labels["input_ids"][i]
            model_inputs["input_ids"][i] = [tokenizer.pad_token_id] * (
                    max_length - len(sample_input_ids)
            ) + sample_input_ids
            model_inputs["attention_mask"][i] = [0] * (max_length - len(sample_input_ids)) + model_inputs[
                "attention_mask"
            ][i]
            labels["input_ids"][i] = [-100] * (max_length - len(sample_input_ids)) + label_input_ids
            model_inputs["input_ids"][i] = torch.tensor(model_inputs["input_ids"][i][:max_length])
            model_inputs["attention_mask"][i] = torch.tensor(model_inputs["attention_mask"][i][:max_length])
            labels["input_ids"][i] = torch.tensor(labels["input_ids"][i][:max_length])
        model_inputs["labels"] = labels["input_ids"]
        return model_inputs

    processed_datasets = dataset.map(
        preprocess_function,
        batched=True,
        num_proc=1,
        remove_columns=dataset["train"].column_names,
        load_from_cache_file=False,
        desc="Running tokenizer on dataset",
    )

    train_dataset = processed_datasets["train"]
    eval_dataset = processed_datasets["train"]

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=default_data_collator, batch_size=batch_size, pin_memory=False
    )
    eval_dataloader = DataLoader(eval_dataset, collate_fn=default_data_collator, batch_size=batch_size,
                                 pin_memory=False)

    model = AutoModelForCausalLM.from_pretrained(model_name_or_path)
    model = get_peft_model(model, peft_config)
    print(model.print_trainable_parameters())

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=0,
        num_training_steps=(len(train_dataloader) * num_epochs),
    )

    for epoch in range(num_epochs):
        model.train()
        total_loss = 0
        for step, batch in enumerate(tqdm(train_dataloader)):
            batch = {k: v for k, v in batch.items()}
            outputs = model(**batch)
            loss = outputs.loss
            total_loss += loss.detach().float()
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

        model.eval()
        eval_loss = 0
        eval_preds = []
        for step, batch in enumerate(tqdm(eval_dataloader)):
            batch = {k: v for k, v in batch.items()}
            with torch.no_grad():
                outputs = model(**batch)
            loss = outputs.loss
            eval_loss += loss.detach().float()
            eval_preds.extend(
                tokenizer.batch_decode(torch.argmax(outputs.logits, -1).detach().cpu().numpy(),
                                       skip_special_tokens=True)
            )

        eval_epoch_loss = eval_loss / len(eval_dataloader)
        eval_ppl = torch.exp(eval_epoch_loss)
        train_epoch_loss = total_loss / len(train_dataloader)
        train_ppl = torch.exp(train_epoch_loss)
        print("epoch=%s: train_ppl=%s train_epoch_loss=%s eval_ppl=%s eval_epoch_loss=%s" % (
        epoch, train_ppl, train_epoch_loss, eval_ppl, eval_epoch_loss))

    from huggingface_hub import login
    token = os.environ.get("HUGGINGFACE_TOKEN")

    login(token=token)

    peft_model_id = peft_model_publish_id
    model.push_to_hub(peft_model_id, use_auth_token=True)


@component(
    packages_to_install=["transformers", "peft", "torch"],
    base_image='python:3.10'
)
def test_prompt_tuning_config(peft_model_id: str, model_name_or_path: str):
    from peft import PeftModel, PeftConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer
    import torch

    tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
    config = PeftConfig.from_pretrained(peft_model_id)
    model = AutoModelForCausalLM.from_pretrained(config.base_model_name_or_path)
    model = PeftModel.from_pretrained(model, peft_model_id)
    text_column = "Tweet text"
    inputs = tokenizer(
        f'{text_column} : {"@nationalgridus I have no water and the bill is current and paid. Can you do something about this?"} Label : ',
        return_tensors="pt",
    )

    with torch.no_grad():
        inputs = {k: v for k, v in inputs.items()}
        outputs = model.generate(
            input_ids=inputs["input_ids"], attention_mask=inputs["attention_mask"], max_new_tokens=10, eos_token_id=3
        )
        print(tokenizer.batch_decode(outputs.detach().cpu().numpy(), skip_special_tokens=True))


# prompt_tuning_bloom_op = comp.func_to_container_op(prompt_tuning_bloom, packages_to_install=['peft', 'transformers', 'datasets'], base_image='python:3.10')
# test_prompt_tuning_config_op = comp.func_to_container_op(test_prompt_tuning_config, packages_to_install=['peft', 'transformers'], base_image='python:3.10')

# Define your pipeline function
@dsl.pipeline(
    name="LLM Prompt tuning pipeline",
    description="A Pipeline for Prompt Tuning LLMs"
)
def prompt_tuning_pipeline(
        peft_model_publish_id: str = "difince/bloomz-560m_PROMPT_TUNING_CAUSAL_LM",
        model_name_or_path: str = "bigscience/bloomz-560m",
        num_epochs: int = 1,
        test_prompt_tuning: str = "true"
):
    print(kfp.__version__)
    prompt_tuning_llm = prompt_tuning_bloom(peft_model_publish_id=peft_model_publish_id,
                                            model_name_or_path=model_name_or_path,
                                            num_epochs=num_epochs)
    # Read the secret containing the HUGGINGFACE_TOKEN
    config.load_kube_config(config_file="/home/dianaa/.kube/config")
    core_api = client.CoreV1Api()
    secret = core_api.read_namespaced_secret(name="huggingface-secret", namespace="kubeflow")
    print(secret.data["token"])

    # prompt_tuning_llm.add_env_variable(env_from_secret('HUGGINGFACE_TOKEN', 'huggingface-secret', 'token'))
    # probably I need to decript base64 the secret?
    # prompt_tuning_llm.add_env_variable(secret.data["token"])
    prompt_tuning_llm.set_env_variable("HUGGINGFACE_TOKEN", secret.data["token"])
    # with I.(test_prompt_tuning == 'true'):
    test_prompt_tuning = test_prompt_tuning_config(peft_model_id=peft_model_publish_id,
                                                   model_name_or_path=model_name_or_path)
    test_prompt_tuning.after(prompt_tuning_llm)
    # test_prompt_tuning.add_pod_label('pipelines.kubeflow.org/cache_enabled', 'false')


# Compile the pipeline
# pipeline_func = prompt_tuning_pipeline
Compiler().compile(prompt_tuning_pipeline, 'prompt_tuning_pipeline.yaml')
