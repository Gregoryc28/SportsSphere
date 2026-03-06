# 1. Use the official Playwright image
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# 2. Set the working directory
WORKDIR /app

# 3. Copy requirements FIRST (for better caching)
COPY requirements.txt .

# 4. Install dependencies from the file
RUN pip install --no-cache-dir -r requirements.txt

# 5. Install Chromium (ensures the browser binary matches the Playwright version)
RUN playwright install chromium

# 6. Copy the rest of your application code
COPY . .

# 7. Expose the port
EXPOSE 8000

# 8. Start the server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]