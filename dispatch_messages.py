"""
Разовый прогон доставки сообщений по расписанию — для внешнего cron/systemd-таймера,
если фоновый цикл в приложении отключён (NEIROMASTER_SCHEDULER=0).

Проходит по всем схемам (public + кабинеты): досоздаёт недостающие строки расписания
и выпускает в кабинет все наступившие сообщения.

    python dispatch_messages.py            # один проход
    */1 * * * *  cd /app && python dispatch_messages.py   # пример строки crontab
"""
import messaging

if __name__ == "__main__":
    delivered = messaging.dispatch_all()
    print(f"Доставлено сообщений: {delivered}")
