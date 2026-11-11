package marine;

import java.time.Duration;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.cloud.gateway.route.RouteLocator;
import org.springframework.cloud.gateway.route.builder.RouteLocatorBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.util.unit.DataSize;

@SpringBootApplication
public class Gateway {
    public static void main(String[] args) { SpringApplication.run(Gateway.class, args); }
    @Bean RouteLocator routes(RouteLocatorBuilder builder) {
        String backend=System.getenv().getOrDefault("MARINE_BACKEND", "http://127.0.0.1:8870");
        return builder.routes().route("classify", route -> route.path("/v1/classifications")
            .filters(f -> f.setRequestSize(DataSize.ofMegabytes(9)).preserveHostHeader()).uri(backend))
            .route("application", route -> route.path("/**")
            .filters(f -> f.setRequestSize(DataSize.ofKilobytes(512)).preserveHostHeader()).uri(backend)).build();
    }
}
