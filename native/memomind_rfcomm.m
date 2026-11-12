// RFCOMM channels to a paired Bluetooth Classic device on macOS (IOBluetooth), for the MemoMind One flasher.
// Every call must come from the process's main thread: channel callbacks arrive on the main run loop, which the
// waits below pump, as the research tool's MacRFCOMMTransport did for the hardware-verified installs.
// mm_open returns a handle > 0 or -1; mm_read returns the byte count, 0 on timeout, -1 once the channel failed;
// mm_write returns 0 or -1. After -1, mm_error describes the failure.
#import <CoreBluetooth/CoreBluetooth.h>
#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>

static char errorText[1024];
// Sessions are never released: IOBluetooth does not retain channel delegates, and a timed-out open or write can
// still call back later.
static NSMutableArray *sessions;

static const NSUInteger ReceiveLimit = 1 << 20;

const char *mm_error(void) { return errorText; }

static int fail(NSString *text) {
  snprintf(errorText, sizeof(errorText), "%s", text.UTF8String);
  return -1;
}

static NSTimeInterval now(void) { return NSProcessInfo.processInfo.systemUptime; }

static BOOL pump(NSTimeInterval deadline, BOOL (^done)(void)) {
  while (!done()) {
    NSTimeInterval remaining = deadline - now();
    if (remaining <= 0) return NO;
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, MIN(remaining, 0.01), true);
  }
  return YES;
}

@interface MMSession : NSObject <IOBluetoothRFCOMMChannelDelegate>
@property(nonatomic, strong) IOBluetoothDevice *device;
@property(nonatomic, strong) IOBluetoothRFCOMMChannel *channel;
@property(nonatomic, strong) NSMutableData *received;
@property(nonatomic, strong) NSMutableData *writing;
@property(nonatomic, copy) NSString *failure;
@property(nonatomic, copy) NSString *source;
@property(nonatomic) BOOL sdpPending, openPending, connected;
@property(nonatomic) BluetoothRFCOMMChannelID channelID;
@end

@implementation MMSession
- (instancetype)init {
  if ((self = [super init])) {
    _received = [NSMutableData data];
    _source = @"";
  }
  return self;
}

- (void)failWith:(NSString *)reason {
  if (!self.failure) self.failure = reason;
  self.connected = NO;
  if (self.channel) [self.channel closeChannel];
}

- (void)sdpQueryComplete:(IOBluetoothDevice *)device status:(IOReturn)status {
  if (device != self.device || !self.sdpPending) return;
  self.sdpPending = NO;
  if (status != kIOReturnSuccess) [self failWith:[NSString stringWithFormat:@"SDP query failed (0x%08x)", status]];
}

- (void)rfcommChannelOpenComplete:(IOBluetoothRFCOMMChannel *)channel status:(IOReturn)status {
  if (!self.openPending) return;
  self.openPending = NO;
  if (!self.channel) self.channel = channel;
  if (status != kIOReturnSuccess) {
    [self failWith:[NSString stringWithFormat:@"RFCOMM open failed (0x%08x)", status]];
  } else if (self.failure) {
    [channel closeChannel];
  } else {
    self.connected = YES;
  }
}

- (void)rfcommChannelClosed:(IOBluetoothRFCOMMChannel *)channel {
  if (channel != self.channel) return;
  self.connected = NO;
  self.writing = nil;
  if (!self.failure) self.failure = @"The glasses closed the Bluetooth channel";
}

- (void)rfcommChannelData:(IOBluetoothRFCOMMChannel *)channel data:(void *)data length:(size_t)length {
  if (channel != self.channel || self.failure || length == 0) return;
  if (!data || self.received.length + length > ReceiveLimit) {
    [self failWith:@"Bluetooth receive buffer overflow"];
    return;
  }
  [self.received appendBytes:data length:length];
}

- (void)rfcommChannelWriteComplete:(IOBluetoothRFCOMMChannel *)channel refcon:(void *)refcon status:(IOReturn)status {
  if (channel != self.channel || !self.writing || refcon != self.writing.mutableBytes) return;
  self.writing = nil;
  if (status != kIOReturnSuccess) [self failWith:[NSString stringWithFormat:@"RFCOMM write failed (0x%08x)", status]];
}
@end

static MMSession *lookup(int handle) {
  if (!NSThread.isMainThread) {
    fail(@"IOBluetooth calls must come from the main thread");
    return nil;
  }
  if (handle < 1 || handle > (int)sessions.count) {
    fail(@"Unknown channel handle");
    return nil;
  }
  return sessions[handle - 1];
}

// Lines of "address<TAB>name\n" for the paired-device cache. Reads local records only; no scan, no connection.
int mm_paired(char *out, int capacity) {
  @autoreleasepool {
    errorText[0] = 0;
    if (!NSThread.isMainThread) return fail(@"IOBluetooth calls must come from the main thread");
    CBManagerAuthorization access = CBManager.authorization;
    if (access == CBManagerAuthorizationDenied || access == CBManagerAuthorizationRestricted) {
      return fail(@"macOS does not allow this app to use Bluetooth. Turn it on for Glassly CFW Flasher in System "
                  @"Settings > Privacy & Security > Bluetooth, then start again.");
    }
    NSMutableString *text = [NSMutableString string];
    NSCharacterSet *breaks = [NSCharacterSet characterSetWithCharactersInString:@"\t\r\n"];
    for (IOBluetoothDevice *device in [IOBluetoothDevice pairedDevices]) {
      NSString *address = device.addressString;
      if (!address) continue;
      NSArray *parts = [(device.name ?: @"") componentsSeparatedByCharactersInSet:breaks];
      NSString *name = [parts componentsJoinedByString:@" "];
      [text appendFormat:@"%@\t%@\n", address, name];
    }
    NSData *bytes = [text dataUsingEncoding:NSUTF8StringEncoding];
    if ((long)bytes.length >= capacity) return fail(@"Too many paired devices to list");
    memcpy(out, bytes.bytes, bytes.length);
    out[bytes.length] = 0;
    return (int)bytes.length;
  }
}

// Opens the RFCOMM channel of the device's SDP record for uuid16 (cached record first, else a fresh query),
// or channelID directly when it is 1...30.
int mm_open(const char *address, int uuid16, int channelID, int timeoutMs) {
  @autoreleasepool {
    errorText[0] = 0;
    if (!NSThread.isMainThread) return fail(@"IOBluetooth calls must come from the main thread");
    if (!sessions) sessions = [NSMutableArray array];
    IOBluetoothDevice *device = [IOBluetoothDevice deviceWithAddressString:@(address)];
    if (!device) return fail(@"Invalid Bluetooth address");
    MMSession *session = [MMSession new];
    session.device = device;
    [sessions addObject:session];
    NSTimeInterval deadline = now() + timeoutMs / 1000.0;
    BluetoothRFCOMMChannelID channel = (BluetoothRFCOMMChannelID)channelID;
    if (channelID < 1 || channelID > 30) {
      IOBluetoothSDPUUID *uuid = [IOBluetoothSDPUUID uuid16:(BluetoothSDPUUID16)uuid16];
      IOBluetoothSDPServiceRecord *record = [device getServiceRecordForUUID:uuid];
      session.source = @"cached-sdp";
      if (!record) {
        session.source = @"fresh-sdp";
        session.sdpPending = YES;
        IOReturn status = [device performSDPQuery:session uuids:@[ uuid ]];
        if (status != kIOReturnSuccess) {
          session.sdpPending = NO;
          return fail([NSString stringWithFormat:@"SDP query could not start (0x%08x)", status]);
        }
        // The research Mac saw a fresh query fill the cache without ever calling its completion.
        if (!pump(deadline, ^{ return (BOOL)(!session.sdpPending || [device getServiceRecordForUUID:uuid]); })) {
          [session failWith:@"SDP query timed out"];
          return fail(@"SDP query timed out; are the glasses switched on and close?");
        }
        if (session.failure) return fail(session.failure);
        record = [device getServiceRecordForUUID:uuid];
      }
      channel = 0;
      if (!record || [record getRFCOMMChannelID:&channel] != kIOReturnSuccess || channel < 1 || channel > 30) {
        return fail([NSString stringWithFormat:@"The glasses do not list Bluetooth service %04x", uuid16]);
      }
    } else {
      session.source = @"channel";
    }
    session.channelID = channel;
    session.openPending = YES;
    IOBluetoothRFCOMMChannel *opened = nil;
    IOReturn status = [device openRFCOMMChannelAsync:&opened withChannelID:channel delegate:session];
    if (status != kIOReturnSuccess) {
      session.openPending = NO;
      return fail([NSString stringWithFormat:@"RFCOMM open could not start (0x%08x)", status]);
    }
    if (!session.channel) session.channel = opened;
    if (!pump(deadline, ^{ return (BOOL)!session.openPending; })) {
      [session failWith:@"RFCOMM open timed out"];
      return fail(@"RFCOMM open timed out; are the glasses switched on and close?");
    }
    if (!session.connected) return fail(session.failure ?: @"RFCOMM channel did not open");
    return (int)sessions.count;
  }
}

int mm_channel(int handle) {
  MMSession *session = lookup(handle);
  return session ? session.channelID : -1;
}

const char *mm_source(int handle) {
  MMSession *session = lookup(handle);
  return session ? session.source.UTF8String : "";
}

// Sends data in MTU-sized writes, each awaited. Any failure closes the channel: the glasses may already hold part
// of the data, so the caller must not resend.
int mm_write(int handle, const char *data, int length, int timeoutMs) {
  @autoreleasepool {
    errorText[0] = 0;
    MMSession *session = lookup(handle);
    if (!session) return -1;
    NSTimeInterval deadline = now() + timeoutMs / 1000.0;
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0, true);
    if (session.failure) return fail(session.failure);
    if (!session.connected) return fail(@"The Bluetooth channel is not open");
    NSUInteger mtu = [session.channel getMTU];
    if (mtu == 0) {
      [session failWith:@"The Bluetooth channel reported a zero MTU"];
      return fail(session.failure);
    }
    int offset = 0;
    while (offset < length) {
      int count = (int)MIN(MIN((NSUInteger)(length - offset), mtu), (NSUInteger)UINT16_MAX);
      session.writing = [NSMutableData dataWithBytes:data + offset length:count];
      IOReturn status = [session.channel writeAsync:session.writing.mutableBytes length:(UInt16)count
                                             refcon:session.writing.mutableBytes];
      if (status != kIOReturnSuccess) {
        session.writing = nil;
        [session failWith:[NSString stringWithFormat:@"RFCOMM write could not start (0x%08x)", status]];
        return fail(session.failure);
      }
      if (!pump(deadline, ^{ return (BOOL)(session.writing == nil || session.failure != nil); })) {
        [session failWith:@"RFCOMM write timed out"];
        return fail(session.failure);
      }
      if (session.failure) return fail(session.failure);
      offset += count;
    }
    return 0;
  }
}

int mm_read(int handle, char *out, int capacity, int timeoutMs) {
  @autoreleasepool {
    errorText[0] = 0;
    MMSession *session = lookup(handle);
    if (!session) return -1;
    NSTimeInterval deadline = now() + timeoutMs / 1000.0;
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0, true);
    BOOL ready = pump(deadline, ^{ return (BOOL)(session.failure != nil || session.received.length > 0); });
    if (session.failure) return fail(session.failure);
    if (!ready) return 0;
    int count = (int)MIN((NSUInteger)capacity, session.received.length);
    memcpy(out, session.received.bytes, count);
    [session.received replaceBytesInRange:NSMakeRange(0, count) withBytes:NULL length:0];
    return count;
  }
}

// Closes this channel only; the baseband link, which the other service's channel may share, stays up.
void mm_close(int handle) {
  @autoreleasepool {
    MMSession *session = lookup(handle);
    if (session) [session failWith:@"The Bluetooth channel was closed"];
  }
}
