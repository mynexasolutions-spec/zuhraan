import os
import re

admin_dir = r"c:\Users\Asus\Desktop\zuhraan\templates\admin"

def repl(m):
    val = float(m.group(1))
    new_val = round(val + 0.2, 3) 
    return f"font-size: {new_val}rem"

for filename in os.listdir(admin_dir):
    if filename.endswith(".html"):
        filepath = os.path.join(admin_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        new_content = re.sub(r'font-size:\s*([0-9\.]+)rem', repl, content)
        
        if new_content != content:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Updated {filename}")
