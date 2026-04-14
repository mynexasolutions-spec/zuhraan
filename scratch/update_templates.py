import os
import re

template_dir = 'c:\\Users\\Asus\\Desktop\\zuhraan\\templates'

# Regex finds {{ url_for('static', filename=VARIABLE) }}
# It avoids capturing 'string' by requiring the first char to be a letter (variable name)
regex = re.compile(r"\{\{\s*url_for\(\s*'static'\s*,\s*filename=\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\)\s*\}\}")

count = 0
for root, dirs, files in os.walk(template_dir):
    for file in files:
        if file.endswith('.html'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            new_content = regex.sub(r"{{ get_image_url(\1) }}", content)
            
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"Updated {filepath}")
                count += 1

# Also, there are raw /static/ bindings like: <img src="/static/{{ img }}">
# We should replace "/static/{{ img }}" with "{{ get_image_url(img) }}"
regex_raw = re.compile(r"/static/\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*(?:\[0\])?)\s*\}\}")
for root, dirs, files in os.walk(template_dir):
    for file in files:
        if file.endswith('.html'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            new_content = regex_raw.sub(r"{{ get_image_url(\1) }}", content)
            
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"Updated RAW {filepath}")

# Edge case: {% if img_list %}/static/{{ img_list[0] }}{% else %}...
regex_complex = re.compile(r"/static/\{\{\s*([a-zA-Z_][a-zA-Z0-9_.\[\]]+)\s*\}\}")
for root, dirs, files in os.walk(template_dir):
    for file in files:
        if file.endswith('.html'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            new_content = regex_complex.sub(r"{{ get_image_url(\1) }}", content)
            
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"Updated COMPLEX {filepath}")

print(f"Done.")
