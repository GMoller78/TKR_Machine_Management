# generate_hash.py
from werkzeug.security import generate_password_hash
import sys

if __name__ == "__main__":
    if len(sys.argv) < 2:
        password = input("Enter the password to hash: ")
    else:
        password = sys.argv[1]

    if not password:
        print("No password provided.")
    else:
        hashed_password = generate_password_hash(password)
        print("Hashed Password:")
        print(hashed_password)