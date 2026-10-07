# Painel de Recuperação (WHOOP)

Painel pessoal com Recovery, HRV, sono, Strain e treinos, atualizado automaticamente pela API oficial do WHOOP.

## Como funciona

- O GitHub Actions (`.github/workflows/atualizar.yml`) roda de hora em hora pela manhã e a cada 3 horas no resto do dia.
- `whoop_sync.py` busca os dados novos e guarda em SQLite.
- `build_dashboard.py` gera a página em `site/` com os dados criptografados (AES-GCM, chave derivada da senha com PBKDF2).
- A página é publicada no GitHub Pages e só abre com a senha.
- O login do WHOOP e o histórico ficam criptografados no branch `state` (`state.enc`), com a chave no secret `STATE_KEY`.

Nada legível sobre saúde fica no repositório ou na página pública.

## Secrets necessários

| Secret | O que é |
|---|---|
| `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` | app criado em developer-dashboard.whoop.com |
| `STATE_KEY` | chave de 32 bytes em base64 (`python state_crypto.py newkey`) |
| `DASH_PASSWORD` | senha que abre o painel |

## Uso local

```bash
python state_crypto.py pull       # baixa e abre o estado mais recente (precisa de state_key no config.json)
python build_dashboard.py --plain # gera site/index.html sem senha para ver localmente
gh workflow run atualizar.yml     # força uma atualização agora
```

Não rode `whoop_sync.py` localmente depois que o Actions estiver ativo: o WHOOP troca o token a cada renovação, e duas cópias renovando ao mesmo tempo derrubam o login.
