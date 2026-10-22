package echo_test

import (
	"context"
	"net"
	"testing"
	"time"

	"cloudwego.local/echo/kitex_gen/echo"
	echoservice "cloudwego.local/echo/kitex_gen/echo/echo"
	"github.com/cloudwego/kitex/client"
	"github.com/cloudwego/kitex/server"
)

type handler struct{}

func (*handler) Ping(_ context.Context, req *echo.Request) (*echo.Response, error) {
	return &echo.Response{Message: "reply:" + req.Message}, nil
}

func TestRPCRoundtrip(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := echoservice.NewServer(&handler{}, server.WithListener(ln), server.WithExitWaitTime(time.Second))
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()
	t.Cleanup(func() {
		if err := srv.Stop(); err != nil {
			t.Errorf("stop: %v", err)
		}
		ln.Close()
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("server: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("server did not stop")
		}
	})
	cli, err := echoservice.NewClient("echo", client.WithHostPorts(ln.Addr().String()), client.WithRPCTimeout(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, message := range []string{"hello", "中文 RPC", ""} {
		var response *echo.Response
		for {
			response, err = cli.Ping(ctx, &echo.Request{Message: message})
			if err == nil {
				break
			}
			select {
			case <-ctx.Done():
				t.Fatalf("RPC failed: %v", err)
			case err := <-done:
				t.Fatalf("server exited: %v", err)
			case <-time.After(50 * time.Millisecond):
			}
		}
		if response == nil || response.Message != "reply:"+message {
			t.Fatalf("response mismatch for %q: %+v", message, response)
		}
		t.Logf("RPC response verified for %q", message)
	}
}
