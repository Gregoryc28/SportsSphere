# 1. Use the official Playwright image
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# 2. Set the working directory
WORKDIR /app

# 3. Copy requirements FIRST (for better caching)
COPY requirements.txt .

# 4. Install Google Chrome so Playwright can use channel="chrome" in production
RUN apt-get update \
	&& apt-get install -y wget gnupg \
	&& wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg \
	&& echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
	&& apt-get update \
	&& apt-get install -y google-chrome-stable \
	&& rm -rf /var/lib/apt/lists/*

# 5. Install dependencies from the file
RUN pip install --no-cache-dir -r requirements.txt

# 6. Install Chromium (kept as fallback if Chrome channel is unavailable)
RUN playwright install chromium

# 7. Copy the rest of your application code
COPY . .

# 8. Expose the port
EXPOSE 8000

# 9. Start the server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]