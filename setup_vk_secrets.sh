#!/bin/bash
# Кладёт VK_TOKEN и VK_GROUP_ID из локального файла .vk.env в секреты GitHub-репозитория.
# Значения нигде не печатаются. Запуск: ./setup_vk_secrets.sh ВАШ_ЛОГИН/имя-репозитория
set -e
REPO="${1:?Укажите репозиторий: ./setup_vk_secrets.sh логин/имя-репозитория}"
ENVF="$(dirname "$0")/.vk.env"
[ -f "$ENVF" ] || { echo "Нет файла $ENVF (см. .vk.env.example)"; exit 1; }
val() { grep "^$1=" "$ENVF" | head -1 | cut -d= -f2-; }
[ -n "$(val VK_TOKEN)" ] && [ -n "$(val VK_GROUP_ID)" ] || { echo "VK_TOKEN и VK_GROUP_ID должны быть заполнены"; exit 1; }
val VK_TOKEN    | gh secret set VK_TOKEN    --repo "$REPO"
val VK_GROUP_ID | gh secret set VK_GROUP_ID --repo "$REPO"
echo "Готово. Секреты в репозитории:"; gh secret list --repo "$REPO"
