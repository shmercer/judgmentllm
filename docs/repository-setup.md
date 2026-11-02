# Creating the public GitHub repository

These commands are intended for a local computer with `git` and GitHub CLI (`gh`) installed and an authenticated `gh` session. Unzip `judgmentllm-spec-starter.zip`; its top-level directory will be `judgmentllm`.

```bash
cd judgmentllm
gh auth status
git init -b main
git add .
git commit -m "Add initial JudgmentLLM design specification"
gh repo create shmercer/judgmentllm --public --source=. --remote=origin --push
```

The tool integration that prepared these files could not create a GitHub repository directly. No GitHub repo was created when this bundle was generated. If the `shmercer/judgmentllm` name is claimed in the meantime, the command will fail rather than overwrite an existing repository.

Next steps on GitHub: add project description, protect `main` after CI is configured, optionally connect Codecov, and open a milestone issue using `docs/implementation-plan.md`. Do not commit real input texts, API keys, run data, or private output artifacts.

For PyPI, confirm the normalized distribution name and use a GitHub Actions Trusted Publisher when ready to publish a first release. Public searches do not reserve a PyPI name.
