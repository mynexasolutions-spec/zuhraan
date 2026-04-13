import os
import re

CSRF_TOKEN = '\n    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">'

def inject_csrf(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.html'):
                filepath = os.path.join(root, file)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Check if it already has csrf_token to avoid duplication
                if 'name="csrf_token"' in content:
                    continue
                
                # We only want to inject into method="POST" forms
                # But to be safe, we can inject into any form that has method="POST" optionally with case-insensitive
                # Re-substitute: find <form ... method="POST" ...> and inject right after
                new_content = re.sub(
                    r'(<form[^>]*method=["\']?POST["\']?[^>]*>)',
                    r'\1' + CSRF_TOKEN,
                    content,
                    flags=re.IGNORECASE
                )
                
                if new_content != content:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    print(f"Injected CSRF into: {filepath}")

if __name__ == '__main__':
    inject_csrf('templates')
