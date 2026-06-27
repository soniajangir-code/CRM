# Use Microsoft's official Playwright Python base image, which has all system-level dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set working directory inside the container
WORKDIR /app

# Copy python dependencies file first to leverage Docker layer caching
COPY requirements.txt .

# Install requirements
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Expose port 5000 for the Waitress server
EXPOSE 5000

# Set environment variable to flag cloud deployment
ENV RENDER=true

# Command to run the Waitress production server
CMD ["python", "app.py"]
