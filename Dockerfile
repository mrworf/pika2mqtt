FROM python:3

ENV HOSTNAME=
ENV MQTT=
ENV BASETOPIC=
ENV IGNORE=
ENV DEBUG=
ENV IDRSA="/key/id_rsa"

WORKDIR /usr/src/app
COPY . ./

RUN pip3 install requests paho-mqtt

CMD /usr/src/app/pika2mqtt.py "$HOSTNAME" "$MQTT" "$BASETOPIC" --idrsa $IDRSA $DEBUG $IGNORE
