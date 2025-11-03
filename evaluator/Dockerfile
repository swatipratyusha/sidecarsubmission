FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy evaluation scripts
COPY src/ ./src/

# Create directories for reports and screenshots
RUN mkdir -p /app/reports /app/screenshots

# Set environment variables
ENV PYTHONPATH=/app
ENV AUTOMATION_API_URL=http://automation:5000

# Run the evaluation script
CMD ["python", "src/evaluator.py"] 