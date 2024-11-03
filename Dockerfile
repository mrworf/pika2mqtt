FROM python:3

ENV HOSTNAME=
ENV MQTT=
ENV MQTT_USER=
ENV MQTT_PASSWORD=
ENV BASETOPIC=
ENV IGNORE=
ENV DEBUG=
ENV IDRSA="/key/id_rsa"

WORKDIR /usr/src/app
COPY . ./

RUN pip3 install requests paho-mqtt

CMD /usr/src/app/pika2mqtt.py --user "$MQTT_USER" --password "$MQTT_PASSWORD" "$HOSTNAME" "$MQTT" "$BASETOPIC" --idrsa $IDRSA $DEBUG $IGNORE
