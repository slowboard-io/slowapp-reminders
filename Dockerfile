# The reminders slowapp: Pillow only, on the slim base (pinned by tag and digest; Dependabot bumps it).
FROM ghcr.io/mike-anderson/slowapp-python:1.1.0@sha256:d1542bced1c247f49dcfd34ed2b1a5b6cd36b8113cb7f5d2699b251b3124daeb
COPY slowapp.toml /app/slowapp.toml
# renderer/assets/fonts is a symlink to the base's /opt/slowapp/fonts.
COPY epd /opt/app/epd
COPY renderer /opt/app/renderer
COPY tools /opt/app/tools
COPY reminders_app /opt/app/reminders_app
ENV PYTHONPATH=/opt/app:/opt/slowapp
RUN python3 -m compileall -q /opt/app \
 && python3 -c "from tools import reminders; from renderer import cards" \
 && test -f /opt/app/renderer/assets/fonts/DejaVuSans.ttf
ENTRYPOINT ["python3", "-m", "reminders_app"]
CMD ["job", "render"]
