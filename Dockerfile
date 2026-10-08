# The reminders slowapp: Pillow only, on the slim base (pinned by tag and digest; Dependabot bumps it).
FROM ghcr.io/mike-anderson/slowapp-python:1.2.0@sha256:5cc196d93d9960643d51eac7a49f46fe4eb9a1c4f68deba0f60579ea9559a532
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
