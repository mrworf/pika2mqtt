FROM python:3

ENV HOSTNAME=
ENV MQTT=
ENV MQTT_USER=
ENV MQTT_PASSWORD=
ENV BASETOPIC=
ENV IGNORE=
ENV DEBUG=
ENV IDRSA="/key/id_rsa"
ENV SSH_HOST_FINGERPRINT=
ENV SSH_PORT=22
ENV SSH_LOCAL_PORT=18080
ENV WEB_ENABLED=false
ENV WEB_WRITE_ENABLED=false
ENV WEB_LISTEN=0.0.0.0
ENV WEB_PORT=8000
ENV WEB_USERNAME=
ENV WEB_PASSWORD=
ENV WEB_PASSWORD_FILE=

WORKDIR /usr/src/app
COPY . ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir requests paho-mqtt

EXPOSE 8000

CMD exec /usr/src/app/pika2mqtt.py --user "$MQTT_USER" --password "$MQTT_PASSWORD" "$HOSTNAME" "$MQTT" "$BASETOPIC" --idrsa "$IDRSA" $DEBUG $IGNORE
