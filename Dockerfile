FROM bcgovimages/von-image:py36-1.11-1
ENV ENABLE_PTVSD 0

COPY ./requirements*.txt ./

USER root
RUN apt-get update
RUN apt-get install -y jq

RUN pip3 install --no-cache-dir -r requirements.txt -r requirements.dev.txt

ADD ./aries_cloudagent ./aries_cloudagent
ADD ./bin ./bin
ADD ./README.md ./README.md
ADD ./setup.py ./setup.py

RUN pip3 install --no-cache-dir -e .[indy]
ENTRYPOINT ["/bin/bash", "-c", " ENDPOINT=$(curl --silent 0.0.0.0:4040/api/tunnels); echo $ENDPOINT; aca-py start -it http 0.0.0.0 3001 -e $ENDPOINT -ot http --auto-accept-requests --debug-connections --invite --invite-role admin --invite-label MediciTrainingDockerAgent --admin 0.0.0.0 3000 --admin-insecure-mode --genesis-url https://raw.githubusercontent.com/sovrin-foundation/sovrin/master/sovrin/pool_transactions_sandbox_genesis --wallet-type indy", "--"]