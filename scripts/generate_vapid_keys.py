"""
Сгенерировать пару ключей VAPID для push-уведомлений курьерам.

Ключи печатаются в консоль, и только. В `.env` их кладёт человек: правило
проекта — `.env` живёт в корне, никуда не копируется и ничем не переписывается.

Запуск:
    python scripts/generate_vapid_keys.py

Дальше добавить в `.env` в корне проекта:

    VAPID_PUBLIC_KEY=<то, что напечатано>
    VAPID_PRIVATE_KEY=<то, что напечатано>
    VAPID_CONTACT=mailto:komdir.barhat@gmail.com

Публичный ключ уезжает в браузер курьера (иначе подписка не оформится),
приватный не покидает сервер. Пара постоянная: при её смене все выданные
подписки становятся недействительными, и курьерам придётся включать
уведомления заново.
"""

import base64
import io
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    print("Нужна библиотека cryptography. Установите зависимости:")
    print("    pip install -r requirements.txt")
    sys.exit(1)


def b64(raw: bytes) -> str:
    """base64url без padding — формат, в котором ключи VAPID ждут браузеры."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def main() -> int:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private_key.public_key().public_numbers()

    # Публичный ключ — несжатая точка кривой: 0x04 + X + Y, по 32 байта
    public_raw = (b"\x04"
                  + public_numbers.x.to_bytes(32, "big")
                  + public_numbers.y.to_bytes(32, "big"))
    private_raw = private_key.private_numbers().private_value.to_bytes(32, "big")

    print("=== Ключи VAPID для push-уведомлений ===\n")
    print("Добавьте в .env в КОРНЕ проекта (и в переменные окружения Amvera):\n")
    print(f"VAPID_PUBLIC_KEY={b64(public_raw)}")
    print(f"VAPID_PRIVATE_KEY={b64(private_raw)}")
    print("VAPID_CONTACT=mailto:komdir.barhat@gmail.com")
    print("\nПриватный ключ не показывайте никому и не коммитьте в git.")
    print("При смене пары все подписки курьеров станут недействительными.")

    # Заодно кладём приватный ключ в PEM — некоторые версии pywebpush умеют
    # только его. Печатаем, а не пишем в файл: секрет на диске нам не нужен.
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    print("\n--- тот же приватный ключ в PEM (обычно не нужен) ---")
    print(pem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
