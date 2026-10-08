import subprocess
import torch

device = "cuda" if torch.cuda.is_available() else "cpu"

txt = "Waarschuwing!, Naderend Tjechisch schip, met tien knopen, op koers vijftien graden, type schip: Unknown, over vierentwintig minuten, kleinste afstand zeshonderddrieennegentig meter, nu op DRIE zeemijl van ons vandaan, in richting achtenzeventig."
command = "tts --text '" +txt+ "' --language_idx 'nl' --speaker_idx 'Craig Gutsy'  --model_name 'tts_models/multilingual/multi-dataset/xtts_v2' --pipe_out  | aplay"
print('\n', command)

result = subprocess.run(command, shell=True, capture_output=True, text=False)

print(result.stdout)