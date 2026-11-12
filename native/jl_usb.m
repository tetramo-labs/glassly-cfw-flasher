// Bulk-Only USB access to a Jieli update-mode controller on macOS (IOUSBHost device capture, needs root).
// jl_open returns 0 when exactly one VID:PID device is captured, -2 when none is connected,
// -3 when more than one is connected, and -1 on any other error (see jl_error).
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import <IOUSBHost/IOUSBHost.h>
#include <unistd.h>

static IOUSBHostDevice *device;
static IOUSBHostInterface *interface;
static IOUSBHostPipe *input, *output;
static char errorText[1024];

const char *jl_error(void) { return errorText; }

static int fail(NSString *stage, NSError *error) {
  snprintf(errorText, sizeof(errorText), "%s: %s", stage.UTF8String, error.description.UTF8String);
  return -1;
}

void jl_close(void) {
  @autoreleasepool {
    input = nil;
    output = nil;
    [interface destroy];
    interface = nil;
    [device destroy];
    device = nil;
  }
}

int jl_open(int vendor, int product) {
  @autoreleasepool {
    jl_close();
    errorText[0] = 0;
    CFMutableDictionaryRef match = [IOUSBHostDevice createMatchingDictionaryWithVendorID:@(vendor) productID:@(product)
        bcdDevice:nil deviceClass:nil deviceSubclass:nil deviceProtocol:nil speed:nil productIDArray:nil];
    io_iterator_t matches = 0;
    if (IOServiceGetMatchingServices(MACH_PORT_NULL, match, &matches) != KERN_SUCCESS) {
      snprintf(errorText, sizeof(errorText), "USB enumeration failed");
      return -1;
    }
    io_service_t svc = IOIteratorNext(matches), extra = IOIteratorNext(matches);
    IOObjectRelease(matches);
    if (extra) {
      IOObjectRelease(extra);
      IOObjectRelease(svc);
      snprintf(errorText, sizeof(errorText), "More than one controller is connected in update mode");
      return -3;
    }
    if (!svc) return -2;
    NSError *error = nil;
    device = [[IOUSBHostDevice alloc] initWithIOService:svc options:IOUSBHostObjectInitOptionsDeviceCapture
                                                  queue:nil error:&error interestHandler:nil];
    IOObjectRelease(svc);
    if (!device) return fail(@"Capture", error);
    if (![device configureWithValue:1 matchInterfaces:NO error:&error]) {
      int r = fail(@"Configure", error);
      jl_close();
      return r;
    }
    for (int attempt = 0; attempt < 50 && !interface; attempt++) {
      io_iterator_t iter = 0;
      IORegistryEntryCreateIterator(device.ioService, kIOServicePlane, kIORegistryIterateRecursively, &iter);
      io_service_t child;
      while ((child = IOIteratorNext(iter))) {
        if (IOObjectConformsTo(child, "IOUSBHostInterface")) {
          interface = [[IOUSBHostInterface alloc] initWithIOService:child options:0 queue:nil error:&error interestHandler:nil];
          IOObjectRelease(child);
          break;
        }
        IOObjectRelease(child);
      }
      IOObjectRelease(iter);
      if (!interface) usleep(20000);
    }
    if (!interface) {
      int r = fail(@"Open interface", error);
      jl_close();
      return r;
    }
    input = [interface copyPipeWithAddress:0x81 error:&error];
    output = [interface copyPipeWithAddress:0x01 error:&error];
    if (!input || !output) {
      int r = fail(@"Open pipes", error);
      jl_close();
      return r;
    }
    return 0;
  }
}

int jl_transfer(int endpoint, void *buffer, int length, int timeoutMs) {
  @autoreleasepool {
    if (length < 0 || length > 65535 || (endpoint != 1 && endpoint != 0x81) || !input || !output) return -1;
    NSMutableData *data = [NSMutableData dataWithLength:length];
    if (endpoint == 1) memcpy(data.mutableBytes, buffer, length);
    NSUInteger count = 0;
    NSError *error = nil;
    BOOL ok = [(endpoint == 1 ? output : input) sendIORequestWithData:data bytesTransferred:&count
                                                    completionTimeout:timeoutMs / 1000.0 error:&error];
    if (!ok) return fail(@"Bulk transfer", error);
    if (count > (NSUInteger)length) return -1;
    if (endpoint == 0x81) memcpy(buffer, data.bytes, count);
    return (int)count;
  }
}
