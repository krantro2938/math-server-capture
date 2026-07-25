# The gateway needs bun (server) + ffmpeg — /api/snapshot.jpg decodes one frame
# off the RTSP stream, since MediaMTX has no still-image endpoint.
FROM oven/bun:1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

EXPOSE 8080
CMD ["bun", "run", "server.ts"]
