import os
import re

def change_currency(directory):
    for root, dirs, files in os.walk(directory):
        if '__pycache__' in root or '.venv' in root or '.git' in root:
            continue
        for file in files:
            if file.endswith('.html') or file.endswith('.py') or file.endswith('.js'):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Replace literal ₹ with ₹
                    # Negative lookahead { to avoid matching JS template litrals like `${variable}`
                    new_content = re.sub(r'\₹(?!\{)', '₹', content)
                    
                    if new_content != content:
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        print(f"Updated currency in: {filepath}")
                except Exception as e:
                    print(f"Could not process {filepath}: {e}")

if __name__ == '__main__':
    change_currency('.')
