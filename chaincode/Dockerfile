FROM golang:1.24 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -mod=vendor -o /marine-chaincode .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /marine-chaincode /marine-chaincode
ENV CHAINCODE_SERVER_ADDRESS=0.0.0.0:9999
ENTRYPOINT ["/marine-chaincode"]
