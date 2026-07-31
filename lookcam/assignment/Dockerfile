# The reader needs bun (server) + ffmpeg (one-frame grab from the RTSP stream).
FROM oven/bun:1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.ts ./

# /app/data is a volume in compose — state.json survives restarts and rebuilds.
ENV DATA_DIR=/app/data
EXPOSE 8091
CMD ["bun", "run", "server.ts"]
