#!/usr/bin/env python3
"""
Nginx Configuration Generator for FundamentalSignals ORACLE v2.1.
Run this to generate a production-ready Nginx config file.
"""
import os
import sys

def generate_nginx_config(domain, port=8000):
    template = f"""server {{
    listen 80;
    server_name {domain};

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket timeouts
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }}

    # Optional: Deny access to .git, etc.
    location ~ /\. {{
        deny all;
    }}
}}
"""
    return template

def main():
    if len(sys.argv) < 2:
        print("Usage: python configure_nginx.py <domain_name> [port]")
        print("Example: python configure_nginx.py oracle.example.com 8000")
        sys.exit(1)

    domain = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    
    config_content = generate_nginx_config(domain, port)
    filename = f"nginx_{domain.replace('.', '_')}.conf"
    
    with open(filename, "w") as f:
        f.write(config_content)
    
    print(f"\n✅ Nginx configuration generated: {filename}")
    print("-" * 40)
    print(config_content)
    print("-" * 40)
    print("\nNext steps (on your Linux server):")
    print(f"1. Copy to sites-available:  sudo cp {filename} /etc/nginx/sites-available/{domain}")
    print(f"2. Enable config:            sudo ln -s /etc/nginx/sites-available/{domain} /etc/nginx/sites-enabled/")
    print(f"3. Test Nginx:               sudo nginx -t")
    print(f"4. Reload Nginx:             sudo systemctl reload nginx")
    print("\nOptional (SSL with Certbot):")
    print(f"   sudo certbot --nginx -d {domain}")

if __name__ == "__main__":
    main()
