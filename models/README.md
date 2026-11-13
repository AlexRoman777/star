# Star model vault

`model-lock.json` is the reviewed production allow-list. It records immutable
Hugging Face revisions, checksummed static assets, and the local-runtime models
whose resolved blobs must be archived after download.

The actual model files do not belong in Git. Store them on any backup location by
mounting it on the machine that owns the models, then run:

```bash
./scripts/archive-model-vault.sh /path/to/backup/star/model-vault/2026-07-20 --include-images
```

Run this on the NVIDIA GPU worker after all model containers are healthy. Run it
again on the Mac with the same destination to add the LM Studio model
directory. The archive excludes API keys and worker credentials. `SHA256SUMS`
detects corruption or accidental replacement.

Keep at least two dated vault generations until the new one has been restored
and tested. Model tags are never considered a backup: Ollama and LM Studio
files are preserved as the exact resolved local artifacts.
