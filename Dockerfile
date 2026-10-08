# The reminders slowapp: Pillow only, on the slim base (pinned by tag and digest; Dependabot bumps it).
FROM ghcr.io/mike-anderson/slowapp-python:1.1.1@sha256:5e5180b252d6e179460e7f6baf868a697a7c424a919a228b420a30e6406354c3
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
