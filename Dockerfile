FROM python:3.7-alpine

ENV url
ENV mqtt
ENV basetopic

WORKDIR /usr/src/app
COPY . ./

#CMD ["./pika2mqtt.py", "$url", "$mqtt", "$basetopic"]
CMD ["$url", "$mqtt", "$basetopic"]
