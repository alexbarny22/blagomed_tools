# agent-sync — установка правил/скиллов и доступ к панели CEZAR

Обвязка складского компьютера SKLAD. Работает поверх папки Яндекс Диска
`%USERPROFILE%\YandexDisk\АГЕНТЫ` (см. её README.md).

| Файл | Назначение |
|---|---|
| `apply.ps1` | раскладывает несекретные правила, скиллы, конфиг Cline и программы из `АГЕНТЫ` |
| `pairing-watch.ps1` | обрабатывает ответ одноразового сопряжения из `АГЕНТЫ\cezar\pairing\in` |
| `cezar-check.ps1` | проверяет SSH-туннель и HTTP-ответ панели CEZAR (порт 4321, `/p/agents-2/`) |
| `cezar-open.ps1` | ярлык «СЕЗАР»: поднимает туннель и открывает панель в браузере |
| `create-cezar-shortcut.ps1` | создаёт ярлык «СЕЗАР» на рабочем столе |
| `cezar-authorize.ps1` | привязка складского ключа к `cezar` через уже авторизованный канал (BatchMode, без обхода аутентификации) |
| `cezar\add-sklad-key.sh` | append-only добавление одного публичного ключа в `~cezar/.ssh/authorized_keys` (есть `--dry-run`) |
| `ssh-config-cezar-vps.txt` | копия SSH-профиля склада с закреплённым host key (несекретно) |
| `run-sync.ps1` | одна точка входа для планировщика (apply + pairing-watch + cezar-check) |
| `install-task.ps1` | регистрирует задачи `Agents2-Sync` и `Agents2-Sync-Logon` |
| `state\apply-last.json` | итог последнего прогона установщика |

## Запуск

```powershell
# разово
powershell -NoProfile -ExecutionPolicy Bypass -File C:\blagomed_tools\agent-sync\apply.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File C:\blagomed_tools\agent-sync\cezar-check.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File C:\blagomed_tools\agent-sync\install-task.ps1
```

Планировщик запускает `run-sync.ps1` каждые 15 минут и при входе в систему.
Лог: `%USERPROFILE%\.cline\data\logs\agents2-sync.log` (ротация при 1 МБ).

## Безопасность

- Секреты не копируются: чёрный список имён и расширений в `apply.ps1`
  (`providers.json`, `secrets.json`, `config.py/ini`, `*.env`, `*.key`, `*.pem`,
  файлы с `token|secret|password` в имени).
- Конфиг Cline — только белый список: `global-settings.json`, `cline_mcp_settings.json`,
  перед перезаписью делается `*.bak-<дата>`.
- Приватные ключи SSH не читаются и не передаются. Сопряжение — только публичным ключом.
- VPS и production не меняются: `pairing-watch.ps1` делает лишь `ssh ... echo`,
  никаких записей на сервере.

## Доступ к панели CEZAR

SSH-профиль `cezar-vps` (`%USERPROFILE%\.ssh\config`): `cezar@46.8.203.58:22`,
ключ `ai-agent-vps-2026_ed25519`, `IdentitiesOnly yes`,
закреплённый host key `HostKeyAlias 46.8.203.58` + `StrictHostKeyChecking yes`
(ED25519 `SHA256:n56umM73A1u7HujwziR8BUP6f5FRFcfVR/dTrFF1Qqw`).

Ярлык «СЕЗАР» → `cezar-open.ps1`: при необходимости поднимает
`ssh -N -L 127.0.0.1:4321:127.0.0.1:4321 cezar-vps`, ждёт порт, проверяет
`http://127.0.0.1:4321/p/agents-2/` и открывает браузер.

Идентификация складского ключа и отзыв без влияния на другие машины:
`АГЕНТЫ\cezar\SKLAD-key-identity.json`; привязка ключа на VPS —
`АГЕНТЫ\cezar\provision\add-sklad-key.sh` (только `/home/cezar/.ssh/authorized_keys`).

`cezar-check.ps1` пишет `АГЕНТЫ\cezar\status\<ХОСТ>-cezar.json`:

- `verdict = tunnel-down` — порт 4321 закрыт (ключ не сопряжён / VPS недоступен);
- `verdict = panel-unreachable` — туннель есть, панель не ответила;
- `verdict = ok` — HTTP 200 от `/api/v1/health`, только этот случай считается подключением.

Текущее состояние SKLAD: `tunnel-down` (SSH: Permission denied (publickey,password)).
