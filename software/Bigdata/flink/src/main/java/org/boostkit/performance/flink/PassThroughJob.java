package org.boostkit.performance.flink;

import org.apache.flink.api.java.utils.ParameterTool;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.sink.v2.DiscardingSink;

/** A finite, stateless DataStream job for validating a standalone Flink deployment. */
public final class PassThroughJob {
    private PassThroughJob() {}

    public static void main(String[] args) throws Exception {
        ParameterTool parameters = ParameterTool.fromArgs(args);
        long records = parameters.getLong("records", 1_000_000L);
        int parallelism = parameters.getInt("parallelism", 1);

        if (records <= 0) {
            throw new IllegalArgumentException("--records must be positive");
        }
        if (parallelism <= 0) {
            throw new IllegalArgumentException("--parallelism must be positive");
        }

        StreamExecutionEnvironment environment =
                StreamExecutionEnvironment.getExecutionEnvironment();
        environment.setParallelism(parallelism);
        environment
                .fromSequence(1L, records)
                .name("Generated records")
                .map(value -> value)
                .name("Pass-through")
                .startNewChain()
                .sinkTo(new DiscardingSink<>())
                .name("Discard sink");
        environment.execute("flink-standalone-pass-through");
    }
}
