FROM python:3.7-alpine

ENV URL=
ENV MQTT=
ENV BASETOPIC=
ENV IGNORE=

WORKDIR /usr/src/app
COPY . ./

RUN pip3 install requests paho-mqtt

CMD /usr/src/app/pika2mqtt.py "$URL" "$MQTT" "$BASETOPIC" $IGNORE
