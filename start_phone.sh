#!/data/data/com.termux/files/usr/bin/bash
# Запуск «мозга» Луны на телефоне: bash start_phone.sh
cd "$(dirname "$0")"
command -v termux-wake-lock >/dev/null && termux-wake-lock
command -v python >/dev/null || pkg install -y python
command -v ollama >/dev/null || pkg install -y ollama
while true; do
  python relay.py "$@"
  code=$?
  [ $code -eq 0 ] && break      # вышли сами (Ctrl+C)
  [ $code -eq 1 ] && break      # ошибка настройки — перезапуск не поможет
  echo "relay упал ($code), перезапуск через 5 с…"; sleep 5
done
