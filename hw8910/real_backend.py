"""The identical, offline generation implementation used by evaluation and HTTP."""
import hashlib
from pathlib import Path


class RealBackend:
    def __init__(self, manifest, root):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from release import canonical, verify_artifacts

        artifact = verify_artifacts(manifest, Path(root))
        self.runtime = manifest['runtime']
        self.loaded_id = manifest['model_id']
        self.runtime_sha256 = manifest['runtime_sha256']
        self.calls = 0
        self.torch = torch
        torch.set_num_threads(4)
        torch.manual_seed(self.runtime['generation']['seed'])
        self.device = self.runtime['device']
        spec = manifest['model_spec']
        base = artifact / 'base'
        token_root = artifact / 'adapter' if spec['kind'] == 'adapter' else base
        self.tokenizer = AutoTokenizer.from_pretrained(token_root, local_files_only=True)
        template = hashlib.sha256(self.tokenizer.chat_template.encode()).hexdigest()
        if template != self.runtime['template_sha256']:
            raise ValueError('Loaded chat template differs from evaluation')
        names = ('tokenizer.json', 'tokenizer_config.json', 'merges.txt', 'vocab.json')
        token_files = {n: hashlib.sha256((token_root / n).read_bytes()).hexdigest()
                       for n in names if (token_root / n).exists()}
        if hashlib.sha256(canonical(token_files)).hexdigest() != self.runtime['tokenizer_sha256']:
            raise ValueError('Loaded tokenizer differs from evaluation')
        self.model = AutoModelForCausalLM.from_pretrained(
            base, local_files_only=True, dtype=getattr(torch, self.runtime['dtype']))
        if spec['kind'] == 'adapter':
            from peft import PeftModel
            self.model = PeftModel.from_pretrained(self.model, artifact / 'adapter', local_files_only=True)
        self.model = self.model.to(self.device).eval()

    def _batch(self, prompt):
        generation = self.runtime['generation']
        text = self.tokenizer.apply_chat_template(
            [{'role': 'system', 'content': generation['system']},
             {'role': 'user', 'content': prompt}],
            tokenize=False, add_generation_prompt=True,
            enable_thinking=generation['enable_thinking'])
        return self.tokenizer(text, return_tensors='pt').to(self.device)

    def validate_prompt(self, prompt):
        if self._batch(prompt)['input_ids'].shape[1] > self.runtime['max_input_tokens']:
            raise ValueError('Prompt exceeds evaluated input token limit')

    def generate(self, prompt, limit):
        generation = self.runtime['generation']
        if type(limit) is not int or limit != generation['max_new_tokens']:
            raise ValueError('The release permits exactly the evaluated generation limit')
        batch = self._batch(prompt)
        if batch['input_ids'].shape[1] > self.runtime['max_input_tokens']:
            raise ValueError('Prompt too long')
        self.calls += 1
        with self.torch.inference_mode():
            result = self.model.generate(
                **batch, max_new_tokens=limit, do_sample=generation['do_sample'],
                pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id)
        tokens = result[0, batch['input_ids'].shape[1]:]
        return {'answer': self.tokenizer.decode(tokens, skip_special_tokens=True),
                'output_tokens': len(tokens)}

    def warmup(self):
        self.generate('Choose the command that lists files: A) ls B) rm. Return the label.',
                      self.runtime['generation']['max_new_tokens'])
