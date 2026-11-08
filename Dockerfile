# Eddy's Typing Showdown: page + leaderboard API in one Windows container
FROM python:3.12-windowsservercore-ltsc2022

WORKDIR C:/app

# Install Flask (the web framework) and Waitress (the web server)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The backend code, and the page files in their own folder
COPY app.py .
COPY index.html sentences.json static/

# Scores are saved here; mount a server folder to keep them
VOLUME C:/app/data

EXPOSE 80
CMD ["python", "app.py"]
