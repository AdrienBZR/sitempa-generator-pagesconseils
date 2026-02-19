import base64
import os
import json

# Define path to credentials
creds_path = '../creds/solocal-poc-f9a485d4ac05.json'
output_path = 'coolify_secret.txt'

try:
    if not os.path.exists(creds_path):
        print(f"Error: Credentials file not found at {creds_path}")
        exit(1)

    with open(creds_path, 'rb') as f:
        # Read absolute bytes
        creds_bytes = f.read()

    # Encode to base64
    encoded_bytes = base64.b64encode(creds_bytes)
    encoded_str = encoded_bytes.decode('utf-8')

    # Save to file
    with open(output_path, 'w') as f:
        f.write(encoded_str)

    print(f"✅ Success! Base64 credentials written to: {output_path}")
    print("Copy the content of this file and paste it into Coolify.")
    print(f"\nCommand to view content:\ncat {output_path}")

except Exception as e:
    print(f"Error: {e}")
