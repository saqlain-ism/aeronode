FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aeronode_sim ./aeronode_sim
RUN useradd --create-home sim && chown -R sim /app
USER sim
CMD ["python", "-m", "aeronode_sim.main"]
