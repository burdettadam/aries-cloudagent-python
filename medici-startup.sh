#!/bin/bash

aca-py start \
    -it http 0.0.0.0 3000 \
    -ot http \
    -e $ENDPOINT \
    --label $AGENT_NAME \
    --auto-accept-requests --auto-ping-connection \
    --auto-respond-credential-proposal --auto-respond-credential-offer --auto-respond-credential-request --auto-store-credential \
    --auto-respond-presentation-proposal --auto-respond-presentation-request --auto-verify-presentation \
    --invite --invite-role admin --invite-label "$AGENT_NAME (admin)" \
    --genesis-url https://raw.githubusercontent.com/sovrin-foundation/sovrin/master/sovrin/pool_transactions_sandbox_genesis \
    --wallet-type indy \
    2>/dev/null
